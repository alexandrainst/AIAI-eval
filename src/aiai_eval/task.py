"""Abstract Task class."""

import logging
import random
import warnings
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Sequence, Tuple, Union

import numpy as np
import torch
import torch.nn as nn
from datasets import Dataset, DownloadMode, load_dataset, load_metric
from spacy.language import Language
from torch.utils.data import DataLoader
from tqdm.auto import tqdm
from transformers.data.data_collator import DataCollator
from transformers.modeling_utils import PreTrainedModel
from transformers.tokenization_utils_base import PreTrainedTokenizerBase

from .co2 import get_carbon_tracker
from .config import EvaluationConfig, ModelConfig, TaskConfig
from .exceptions import (
    InvalidEvaluation,
    InvalidFramework,
    ModelNotTrainedForTask,
    MPSFallbackNotEnabled,
    PreprocessingFailed,
    UnsupportedModelType,
    WrongFeatureColumnName,
)
from .hf_hub import get_model_config
from .metric_configs import EMISSIONS, POWER
from .model_loading import load_model
from .scoring import log_scores
from .utils import clear_memory, enforce_reproducibility, has_floats

# Set up a logger
logger = logging.getLogger(__name__)


class Task(ABC):
    """Abstract evaluation task class.

    Args:
        task_config (TaskConfig):
            The configuration of the task.
        evaluation_config (EvaluationConfig):
            The configuration of the evaluation.

    Attributes:
        task_config (TaskConfig):
            The configuration of the task.
        evaluation_config (EvaluationConfig):
            The configuration of the evaluation.
    """

    def __init__(self, task_config: TaskConfig, evaluation_config: EvaluationConfig):
        self.task_config = task_config
        self.evaluation_config = evaluation_config

        # Load the metric functions from the `datasets` library
        self._metrics = {
            metric_cfg.name: load_metric(metric_cfg.huggingface_id)
            for metric_cfg in task_config.metrics
        }

    def evaluate(self, model_id: str) -> Union[Dict[str, Dict[str, float]], str]:
        """Evaluate a model.

        Args:
            model_id (str):
                The full Hugging Face Hub path to the pretrained transformer model. The
                specific model version to use can be added after the suffix '@':
                "model_id@v1.0.0". It can be a branch name, a tag name, or a commit id.

        Returns:
            dict:
                The keys in the dict are 'raw' and 'total', with all the raw scores in
                the first dictionary and the aggregated scores in the second.
        """
        # Fetch the model config
        model_config = get_model_config(
            model_id=model_id, evaluation_config=self.evaluation_config
        )

        # Set random seeds to enforce reproducibility of the randomly initialised
        # weights
        rng = enforce_reproducibility(framework=model_config.framework)

        # Load the model
        model_dict = load_model(
            model_config=model_config,
            task_config=self.task_config,
            evaluation_config=self.evaluation_config,
        )

        # Prepare carbon tracker
        if self.evaluation_config.track_carbon_emissions:
            self.carbon_tracker = get_carbon_tracker(
                task_name=self.task_config.name,
                country_iso_code=self.evaluation_config.country_iso_code,
                verbose=self.evaluation_config.verbose,
            )

        # Load the dataset
        dataset = self._load_data()

        # Remove empty examples from the datasets
        for feat_column in self.task_config.feature_column_names:
            try:
                dataset = dataset.filter(lambda record: len(record[feat_column]) > 0)
            except KeyError:
                raise WrongFeatureColumnName(feat_column)

        # Set variable with number of iterations
        num_iter = 10 if not self.evaluation_config.testing else 2

        if model_config.framework in {"pytorch", "jax"}:
            return self._evaluate_pytorch_jax(
                model_dict=model_dict,
                dataset=dataset,
                rng=rng,
                model_config=model_config,
                num_iter=num_iter,
            )

        elif model_config.framework == "spacy":
            return self._evaluate_spacy(
                model_dict=model_dict,
                dataset=dataset,
                rng=rng,
                model_config=model_config,
                num_iter=num_iter,
            )

        else:
            raise InvalidFramework(model_config.framework)

    def _evaluate_pytorch_jax(
        self,
        model_dict: dict,
        dataset: Dataset,
        rng: np.random.Generator,
        model_config: ModelConfig,
        num_iter: int,
    ) -> Union[Dict[str, Dict[str, float]], str]:
        """Evaluate a PyTorch or JAX model.

        Args:
            model_dict (dict):
                The model dictionary, with keys "model" and "tokenizer".
            dataset (Dataset):
                The test dataset.
            rng (np.random.Generator):
                The random number generator, used to generate bootstrapped versions of
                the test dataset.
            model_config (ModelConfig):
                The model configuration.
            num_iter (int):
                The number of bootstrapped samples of the test dataset to use.

        Returns:
            str or dict:
                If the `only_return_log` is set then a string is returned containing
                the logged evaluation results. Otherwise, a nested dictionary of the
                evaluation results. The keys are the names of the datasets, with values
                being new dictionaries having the model IDs as keys.
        """
        # Extract the model and tokenizer
        model = model_dict["model"]
        tokenizer = model_dict["tokenizer"]

        # Log the number of parameters in the model
        num_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
        logger.info(f"Number of model parameters: {num_params:,}")

        # If we are testing then truncate the test set
        if self.evaluation_config.testing:
            dataset = dataset.select(range(4))

        # Get bootstrapped datasets
        bootstrapped_datasets = [
            Dataset.from_dict(dataset[rng.integers(0, len(dataset), len(dataset))])
            for _ in range(num_iter)
        ]

        # Preprocess the bootstrapped datasets
        try:
            prepared_datasets = [
                self._preprocess_data(
                    bootstrapped_dataset,
                    framework="pytorch",
                    model_config=model.config,
                    tokenizer=tokenizer,
                )
                for bootstrapped_dataset in bootstrapped_datasets
            ]

        # If the preprocessing failed then raise an error
        except ValueError:
            raise PreprocessingFailed()

        # Set up progress bar
        if self.evaluation_config.progress_bar:
            itr = tqdm(range(num_iter), desc="Evaluating")
        else:
            itr = range(num_iter)

        # Load the data collator
        data_collator = self._load_data_collator(tokenizer)

        scores = list()
        for idx in itr:
            while True:
                test_itr_scores_or_err = self._evaluate_pytorch_jax_single_iteration(
                    idx=idx,
                    model_config=model_config,
                    dataset=bootstrapped_datasets[idx],
                    prepared_dataset=prepared_datasets[idx],
                    data_collator=data_collator,
                )

                # If the iteration was successful then break the while-loop
                if isinstance(test_itr_scores_or_err, dict):
                    break

                # Otherwise we encountered an error
                else:
                    raise InvalidEvaluation(
                        "An unknown error occurred during the evaluation of the "
                        f"{idx} iteration. The error message returned was: "
                        f"{str(test_itr_scores_or_err)}"
                    )

            scores.append(test_itr_scores_or_err)

        # If track_carbon_emissions is true append metrics, to correctly log emissions
        # data. We avoid mutating, so any downstream evaluations will not try to use
        # these.
        metric_configs = list(self.task_config.metrics)
        if self.evaluation_config.track_carbon_emissions:
            metric_configs.append(EMISSIONS)
            metric_configs.append(POWER)

        # Log scores
        all_scores = log_scores(
            task_name=self.task_config.pretty_name,
            metric_configs=metric_configs,
            scores=scores,
            model_id=model_config.model_id,
            only_return_log=self.evaluation_config.only_return_log,
        )
        return all_scores

    def _evaluate_pytorch_jax_single_iteration(
        self,
        idx: int,
        model_config: ModelConfig,
        dataset: Dataset,
        prepared_dataset: Dataset,
        data_collator: DataCollator,
    ) -> Union[dict, Exception]:
        """Run a single iteration of a PyTorch/JAX benchmark.

        Args:
            idx (int):
                The index of the current iteration.
            model_config (ModelConfig):
                The model configuration.
            dataset (Dataset):
                The raw test dataset.
            prepared_dataset (Dataset):
                The preprocessed test dataset.
            data_collator (DataCollator):
                The data collator.

        Returns:
            dict or Exception:
                The keys in the dict correspond to the metrics and values
                the corresponding values.
        """
        try:
            # Set random seeds to enforce reproducibility of the randomly
            # initialised weights
            random.seed(703 + idx)
            np.random.seed(703 + idx)
            torch.manual_seed(703 + idx)
            torch.cuda.manual_seed_all(703 + idx)

            # Reinitialise a new model
            model_dict = load_model(
                model_config=model_config,
                task_config=self.task_config,
                evaluation_config=self.evaluation_config,
            )
            model = model_dict["model"]
            tokenizer = model_dict["tokenizer"]

            # Define batch size, which depends on whether we are testing or not
            batch_size = 2 if self.evaluation_config.testing else 32

            # Create dataloader
            dataloader = DataLoader(
                prepared_dataset,
                batch_size=batch_size,
                shuffle=False,
                collate_fn=data_collator,
            )

            # Create progress bar
            if self.evaluation_config.progress_bar:
                itr = tqdm(
                    dataloader, desc=f"Evaluating iteration {idx+1}", leave=False
                )
            else:
                itr = dataloader

            # Start carbon emissions tracking
            if self.evaluation_config.track_carbon_emissions:
                self.carbon_tracker.start()

            # Get model predictions
            all_predictions = list()
            with torch.no_grad():
                for batch in itr:

                    # Prepare the batch
                    batch = self._prepare_batch(batch)

                    # Get the model predictions
                    model_predictions = self._get_model_predictions(
                        model=model,
                        batch=batch,
                    )

                    # Move the predictions back to the CPU and convert it to a NumPy
                    # array
                    model_predictions = model_predictions.cpu().numpy().tolist()

                    # Collect predictions
                    all_predictions.extend(model_predictions)

            # Perform post-processing of predictions
            prepared_predictions_and_labels = self._prepare_predictions_and_labels(
                predictions=all_predictions,
                dataset=dataset,
                prepared_dataset=prepared_dataset,
                model_id2label=model.config.id2label,
                cls_token_index=tokenizer.cls_token_id,
            )

            # If there are multiple metrics but only one pair in the
            # `all_predictions_labels` list, we copy our that entry to ensure there is a
            # pair for each metric
            if (
                len(prepared_predictions_and_labels) == 1
                and len(self.task_config.metrics) > 1
            ):
                prepared_predictions_and_labels *= len(self.task_config.metrics)

            # In the first iteration we do a check to see if the model outputs
            # fit the expected format. If not, we raise an exception.
            if idx == 0:
                if not self._check_if_model_is_trained_for_task(
                    model_predictions=model_predictions
                ):
                    raise ModelNotTrainedForTask(
                        task=self.task_config.name, framework=model_config.framework
                    )

            # Compute the metrics for each prediction batch
            scores = self._compute_metrics(
                predictions_and_labels=prepared_predictions_and_labels,
            )

            # Stop carbon emissions tracking and store emission metrics
            if self.evaluation_config.track_carbon_emissions:
                self.carbon_tracker.stop()
                emissions_data = self.carbon_tracker.final_emissions_data
                factor = 1_000_000 / len(prepared_dataset)
                scores["carbon_emissions"] = factor * emissions_data.emissions
                scores["energy_consumed"] = factor * emissions_data.energy_consumed

            return scores

        except (RuntimeError, ValueError, IndexError) as e:
            if "PYTORCH_ENABLE_MPS_FALLBACK" in str(e):
                raise MPSFallbackNotEnabled()

            # Prevent memory leaks
            try:
                del model
            except UnboundLocalError:
                pass
            try:
                del model_dict
            except UnboundLocalError:
                pass
            clear_memory()

            # Return the error if it wasn't caught by the above conditionals
            return e

    def _evaluate_spacy(
        self,
        model_dict: dict,
        dataset: Dataset,
        rng: np.random.Generator,
        model_config: ModelConfig,
        num_iter: int,
    ) -> Union[Dict[str, Dict[str, float]], str]:
        """Evaluate a PyTorch or JAX model.

        Args:
            model_dict (dict):
                The model dictionary, with keys "model" and "tokenizer".
            dataset (Dataset):
                The test dataset.
            rng (np.random.Generator):
                The random number generator, used to generate bootstrapped versions of
                the test dataset.
            model_config (ModelConfig):
                The model configuration.
            num_iter (int):
                The number of bootstrapped samples of the test dataset to use.

        Returns:
            dict:
                The keys in the dict are 'raw' and 'total', with all the raw scores in
                the first dictionary and the aggregated scores in the second.
        """
        # Extract the model and tokenizer
        model = model_dict["model"]

        # If we are testing then truncate the test set
        if self.evaluation_config.testing:
            dataset = dataset.select(range(4))

        # Get bootstrapped datasets
        bootstrapped_datasets = [
            Dataset.from_dict(dataset[rng.integers(0, len(dataset), len(dataset))])
            for _ in range(num_iter)
        ]

        # Preprocess the bootstrapped datasets
        try:
            prepared_datasets = [
                self._preprocess_data(
                    bootstrapped_dataset,
                    framework="spacy",
                    model_config=model.config,
                )
                for bootstrapped_dataset in bootstrapped_datasets
            ]

        # If the preprocessing failed then raise an error
        except ValueError:
            raise PreprocessingFailed()

        # Set up progress bar
        if self.evaluation_config.progress_bar:
            itr = tqdm(range(num_iter), desc="Evaluating")
        else:
            itr = range(num_iter)

        scores = list()
        for idx in itr:
            while True:
                test_itr_scores = self._evaluate_spacy_single_iteration(
                    idx=idx,
                    model_config=model_config,
                    dataset=bootstrapped_datasets[idx],
                    prepared_dataset=prepared_datasets[idx],
                )
                # If the iteration was successful then break the while-loop
                if isinstance(test_itr_scores, dict):
                    break

                # Otherwise we encountered an error
                else:
                    raise InvalidEvaluation(
                        "An unknown error occurred during the evaluation of the "
                        f"{idx} iteration. The error message returned was: "
                        f"{str(test_itr_scores)}"
                    )

            scores.append(test_itr_scores)

        # If track_carbon_emissions is true append metrics, to correctly log emissions
        # data. We avoid mutating, so any downstream evaluations will not try to use
        # these.
        metric_configs = list(self.task_config.metrics)
        if self.evaluation_config.track_carbon_emissions:
            metric_configs.append(EMISSIONS)
            metric_configs.append(POWER)

        # Log scores
        all_scores = log_scores(
            task_name=self.task_config.pretty_name,
            metric_configs=metric_configs,
            scores=scores,
            model_id=model_config.model_id,
            only_return_log=self.evaluation_config.only_return_log,
        )
        return all_scores

    def _evaluate_spacy_single_iteration(
        self,
        idx: int,
        model_config: ModelConfig,
        dataset: Dataset,
        prepared_dataset: Dataset,
    ) -> Union[dict, Exception]:
        """Run a single iteration of a PyTorch/JAX benchmark.

        Args:
            idx (int):
                The index of the current iteration.
            model_config (ModelConfig):
                The model configuration.
            dataset (Dataset):
                The raw test dataset.
            prepared_dataset (Dataset):
                The preprocessed test dataset.

        Returns:
            dict or Exception:
                The keys in the dict correspond to the metrics and values
                the corresponding values.
        """
        try:
            # Set random seeds to enforce reproducibility of the randomly
            # initialised weights
            random.seed(703 + idx)
            np.random.seed(703 + idx)

            # Reinitialise a new model
            model_dict = load_model(
                model_config=model_config,
                task_config=self.task_config,
                evaluation_config=self.evaluation_config,
            )
            model = model_dict["model"]

            # Define batch size, which depends on whether we are testing or not
            batch_size = 2 if self.evaluation_config.testing else 32

            # Start carbon emissions tracking
            if self.evaluation_config.track_carbon_emissions:
                self.carbon_tracker.start()

            # Get model predictions
            model_predictions = self._get_spacy_predictions(
                model=model, prepared_dataset=prepared_dataset, batch_size=batch_size
            )

            # In the first iteration we do a check to see if the model outputs
            # fit the expected format. If not, we raise an exception.
            if idx == 0:
                if not self._check_if_model_is_trained_for_task(
                    model_predictions=model_predictions
                ):
                    raise ModelNotTrainedForTask(
                        task=self.task_config.name, framework=model_config.framework
                    )

            # Perform post-processing of predictions
            prepared_predictions_and_labels = self._prepare_predictions_and_labels(
                predictions=model_predictions,
                dataset=dataset,
                prepared_dataset=prepared_dataset,
            )

            # If there are multiple metrics but only one pair in the
            # `all_predictions_labels` list, we copy our that entry to ensure there is a
            # pair for each metric
            if (
                len(prepared_predictions_and_labels) == 1
                and len(self.task_config.metrics) > 1
            ):
                prepared_predictions_and_labels *= len(self.task_config.metrics)

            # Compute the metrics for each prediction batch
            scores = self._compute_metrics(
                predictions_and_labels=prepared_predictions_and_labels,
            )

            # Stop carbon emissions tracking and store emission metrics
            if self.evaluation_config.track_carbon_emissions:
                self.carbon_tracker.stop()
                emissions_data = self.carbon_tracker.final_emissions_data
                factor = 1_000_000 / len(prepared_dataset)
                scores["carbon_emissions"] = factor * emissions_data.emissions
                scores["energy_consumed"] = factor * emissions_data.energy_consumed

            return scores

        except (RuntimeError, ValueError, IndexError) as e:
            if "PYTORCH_ENABLE_MPS_FALLBACK" in str(e):
                raise MPSFallbackNotEnabled()

            # Prevent memory leaks
            try:
                del model
            except UnboundLocalError:
                pass
            try:
                del model_dict
            except UnboundLocalError:
                pass
            clear_memory()

            # Return the error if it wasn't caught by the above conditionals
            return e

    def _compute_metrics(
        self,
        predictions_and_labels: List[Tuple[list, list]],
    ) -> Dict[str, float]:
        """Compute the metrics needed for evaluation.

        Args:
            predictions_and_labels (list of pairs of lists):
                The predictions and labels for each metric.

        Returns:
            dict:
                A dictionary with the names of the metrics as keys and the metric
                values as values.
        """
        # Iterate over the predictions, labels and associated metrics
        results = dict()
        for metric_cfg, (predictions, labels) in zip(
            self.task_config.metrics, predictions_and_labels
        ):

            # Load the metric
            metric = self._metrics[metric_cfg.name]

            # Compute the metrics. Sometimes a `RuntimeWarning` is displayed, e.g.,
            # when the predictions are all the same. We ignore this warning.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", category=RuntimeWarning)
                score_dict = metric.compute(
                    predictions=predictions,
                    references=labels,
                    **metric_cfg.compute_kwargs,
                )

            # Add scores to the `results` dictionary
            if score_dict is not None:
                results[metric_cfg.name] = score_dict[metric_cfg.results_key]

        # Return the results
        return results

    def _prepare_predictions_and_labels(
        self,
        predictions: Sequence,
        dataset: Dataset,
        prepared_dataset: Dataset,
        **kwargs,
    ) -> List[Tuple[list, list]]:
        """Prepare predictions and labels for output.

        Args:
            predictions (sequence of either ints or floats):
                The predictions of the model.
            dataset (Dataset):
                The raw dataset.
            prepared_dataset (Dataset):
                The prepared dataset.
            kwargs:
                Extra keyword arguments containing objects used in preparing the
                predictions and labels.

        Returns:
            list of pairs of lists:
                The prepared predictions and labels.
        """
        # Collapse the logits into single predictions for every sample
        if has_floats(predictions):
            predictions = np.argmax(predictions, axis=-1)

        # Extract labels from dataset
        labels = prepared_dataset["labels"]

        # Return the predictions and labels
        return [(list(predictions), list(labels))]

    def __call__(self, *args, **kwargs):
        return self.evaluate(*args, **kwargs)

    def _load_data(self) -> Dataset:
        """Load the dataset.

        Returns:
            Dataset:
                The dataset.

        Raises:
            InvalidEvaluation:
                If the split names specified are incorrect.
        """
        # Set the dataset to redownload if we are running unit tests
        download_mode: Optional[DownloadMode] = None
        if self.evaluation_config.testing:
            download_mode = DownloadMode.FORCE_REDOWNLOAD

        # Load the dataset
        return load_dataset(
            path=self.task_config.huggingface_id,
            name=self.task_config.huggingface_subset,
            use_auth_token=self.evaluation_config.use_auth_token,
            cache_dir=self.evaluation_config.cache_dir,
            download_mode=download_mode,
            split=self.task_config.test_name,
        )

    def _prepare_batch(self, batch: dict) -> dict:
        """Prepare a batch for the model.

        Args:
            batch (dict):
                The batch.

        Returns:
            dict:
                The prepared batch.
        """
        # Move the tensors to the correct device
        batch = {
            key: value.to(self.evaluation_config.device) for key, value in batch.items()
        }

        # Create a view of the batch with only desired features
        accepted_transformer_features = [
            "input_ids",
            "attention_mask",
            "token_type_ids",
        ]
        batch = {
            key: value
            for key, value in batch.items()
            if key in accepted_transformer_features
        }

        # Return the prepared batch
        return batch

    def _get_model_predictions(self, model, batch: dict) -> torch.tensor:
        """Get the predictions of the model.

        Args:
            model (torch.nn.Module):
                The model.
            batch (dict):
                The batch.

        Returns:
            torch.tensor:
                The model predictions.

        Raises:
            UnsupportedModelType:
                If the model type is not supported.
        """
        # If we are dealing with a Hugging Face model then we will use the
        # entire batch dictionary
        if isinstance(model, PreTrainedModel):

            # Get the model predictions
            model_predictions = model(**batch)

            # If we are dealing with a classification model then we will
            # take the logits
            if hasattr(model_predictions, "logits"):
                model_predictions = model_predictions.logits

            # If we are dealing with a question answering model then we
            # will take the start and end logits and merge them
            elif hasattr(model_predictions, "start_logits") and hasattr(
                model_predictions, "end_logits"
            ):
                model_predictions = torch.stack(
                    [
                        model_predictions.start_logits,
                        model_predictions.end_logits,
                    ],
                    dim=-1,
                )

            # Otherwise, we raise an error
            else:
                raise ValueError(
                    "The model predictions are not in the correct format."
                    f"Received outputs with keys {model_predictions.keys()}"
                )

        # If we are dealing with a PyTorch model, then we will only use the
        # input_ids
        elif isinstance(model, nn.Module):
            model_predictions = model(batch["input_ids"])

        # Otherwise, we throw an error
        else:
            model_type = str(type(model))
            raise UnsupportedModelType(model_type=model_type)

        # Return the model predictions
        return model_predictions

    @abstractmethod
    def _preprocess_data(self, dataset: Dataset, framework: str, **kwargs) -> Dataset:
        """Preprocess the data.

        Args:
            dataset (Dataset):
                The dataset.
            framework (str):
                The framework of the model.
            kwargs:
                Extra keyword arguments containing objects used in preprocessing the
                dataset.

        Returns:
            Hugging Face Dataset:
                The preprocessed dataset.
        """
        pass

    @abstractmethod
    def _get_spacy_predictions(
        self, model: Language, prepared_dataset: Dataset, batch_size: int
    ) -> list:
        """Get predictions from SpaCy model on dataset.

        Args:
            model (spaCy Language):
                The model.
            prepared_dataset (Hugging Face dataset):
                The dataset.
            batch_size (int):
                The batch size to use.

        Returns:
            list:
                The predictions.
        """
        pass

    @abstractmethod
    def _load_data_collator(self, tokenizer: PreTrainedTokenizerBase):
        """Load the data collator used to prepare samples during finetuning.

        Args:
            tokenizer (Hugging Face tokenizer or None, optional):
                A pretrained tokenizer. Can be None if the tokenizer is not used in the
                initialisation of the data collator. Defaults to None.

        Returns:
            Hugging Face data collator:
                The data collator.
        """
        pass

    @abstractmethod
    def _check_if_model_is_trained_for_task(self, model_predictions: list) -> bool:
        """Check if the model is trained for the task.

        Args:
            model_predictions (list):
                The model predictions.

        Returns:
            bool:
                Whether the model is trained for the task.
        """
        pass
