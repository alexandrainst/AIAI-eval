"""Unit tests for the `exceptions` module."""

import pytest

from src.aiai_eval.exceptions import (
    HuggingFaceHubDown,
    InvalidEvaluation,
    InvalidFramework,
    MissingLabel,
    ModelDoesNotExistOnHuggingFaceHub,
    ModelFetchFailed,
    NoInternetConnection,
    PreprocessingFailed,
    UnsupportedModelType,
)


class TestInvalidEvaluation:
    """Unit tests for the InvalidEvaluation exception class."""

    @pytest.fixture(scope="class")
    def message(self):
        yield "Test message."

    @pytest.fixture(scope="class")
    def exception(self, message):
        yield InvalidEvaluation(message=message)

    def test_invalid_evaluation_is_an_exception(self, exception):
        with pytest.raises(InvalidEvaluation):
            raise exception

    def test_message_is_stored(self, exception, message):
        assert exception.message == message


class TestModelDoesNotExistOnHuggingFaceHub:
    """Unit tests for the ModelDoesNotExistOnHuggingFaceHub exception class."""

    @pytest.fixture(scope="class")
    def model_id(self):
        yield "test_model_id"

    @pytest.fixture(scope="class")
    def exception(self, model_id):
        yield ModelDoesNotExistOnHuggingFaceHub(model_id=model_id)

    def test_model_does_not_exist_on_hugging_face_hub_is_an_exception(self, exception):
        with pytest.raises(ModelDoesNotExistOnHuggingFaceHub):
            raise exception

    def test_model_id_is_stored(self, exception, model_id):
        assert exception.model_id == model_id

    def test_message_is_stored(self, exception, model_id):
        message = f"The model {model_id} does not exist on the Hugging Face Hub."
        assert exception.message == message


class TestModelFetchFailed:
    """Unit tests for the ModelFetchFailed exception class."""

    @pytest.fixture(scope="class")
    def model_id(self):
        yield "test_model_id"

    @pytest.fixture(scope="class")
    def error_msg(self):
        yield "test_error_msg"

    @pytest.fixture(scope="class")
    def message(self):
        yield "Test message"

    @pytest.fixture(scope="class")
    def exception_without_message(self, model_id, error_msg):
        yield ModelFetchFailed(model_id=model_id, error_msg=error_msg)

    @pytest.fixture(scope="class")
    def exception_with_message(self, model_id, error_msg, message):
        yield ModelFetchFailed(model_id=model_id, error_msg=error_msg, message=message)

    def test_model_fetch_failed_is_an_exception(self, exception_with_message):
        with pytest.raises(ModelFetchFailed):
            raise exception_with_message

    def test_model_id_is_stored(self, exception_with_message, model_id):
        assert exception_with_message.model_id == model_id

    def test_error_msg_is_stored(self, exception_with_message, error_msg):
        assert exception_with_message.error_msg == error_msg

    def test_message_is_stored_if_nonempty(self, exception_with_message, message):
        assert exception_with_message.message == message

    def test_message_is_stored_if_empty(
        self, exception_without_message, model_id, error_msg
    ):
        message = (
            f"Download of {model_id} from the Hugging Face Hub failed, with "
            f"the following error message: {error_msg}."
        )
        assert exception_without_message.message == message


class TestInvalidFramework:
    """Unit tests for the InvalidFramework exception class."""

    @pytest.fixture(scope="class")
    def framework(self):
        yield "test_framework"

    @pytest.fixture(scope="class")
    def exception(self, framework):
        yield InvalidFramework(framework=framework)

    def test_invalid_framework_is_an_exception(self, exception):
        with pytest.raises(InvalidFramework):
            raise exception

    def test_framework_is_stored(self, exception, framework):
        assert exception.framework == framework

    def test_message_is_stored(self, exception, framework):
        message = f"The framework {framework} is not supported."
        assert exception.message == message


class TestPreprocessingFailed:
    """Unit tests for the PreprocessingFailed exception class."""

    @pytest.fixture(scope="class")
    def message(self):
        yield "Test message."

    @pytest.fixture(scope="class")
    def exception(self, message):
        yield PreprocessingFailed(message=message)

    @pytest.fixture(scope="class")
    def exception_with_default_message(self):
        yield PreprocessingFailed()

    def test_preprocessing_failed_is_an_exception(self, exception):
        with pytest.raises(PreprocessingFailed):
            raise exception

    def test_message_is_stored(self, exception, message):
        assert exception.message == message

    def test_default_message_is_stored(self, exception_with_default_message):
        message = "Preprocessing of the dataset could not be done."
        assert exception_with_default_message.message == message


class TestMissingLabel:
    """Unit tests for the MissingLabel exception class."""

    @pytest.fixture(scope="class")
    def label(self):
        yield "TestLabel"

    @pytest.fixture(scope="class")
    def label2id(self):
        yield dict(TestLabel=0)

    @pytest.fixture(scope="class")
    def exception(self, label, label2id):
        yield MissingLabel(label=label, label2id=label2id)

    def test_missing_label_is_an_exception(self, exception):
        with pytest.raises(MissingLabel):
            raise exception

    def test_label_is_stored(self, exception, label):
        assert exception.label == label

    def test_label2id_is_stored(self, exception, label2id):
        assert exception.label2id == label2id

    def test_message_is_stored(self, exception, label, label2id):
        message = (
            f"One of the labels in the dataset, {label}, does not occur in the "
            f"label2id dictionary {label2id}."
        )
        assert exception.message == message


class TestHuggingFaceHubDown:
    """Unit tests for the HuggingFaceHubDown exception class."""

    @pytest.fixture(scope="class")
    def message(self):
        yield "Test message."

    @pytest.fixture(scope="class")
    def exception(self, message):
        yield HuggingFaceHubDown(message=message)

    @pytest.fixture(scope="class")
    def exception_with_default_message(self):
        yield HuggingFaceHubDown()

    def test_hugging_face_hub_down_is_an_exception(self, exception):
        with pytest.raises(HuggingFaceHubDown):
            raise exception

    def test_message_is_stored(self, exception, message):
        assert exception.message == message

    def test_default_message_is_stored(self, exception_with_default_message):
        message = "The Hugging Face Hub is currently down."
        assert exception_with_default_message.message == message


class TestNoInternetConnection:
    """Unit tests for the NoInternetConnection exception class."""

    @pytest.fixture(scope="class")
    def message(self):
        yield "Test message."

    @pytest.fixture(scope="class")
    def exception(self, message):
        yield NoInternetConnection(message=message)

    @pytest.fixture(scope="class")
    def exception_with_default_message(self):
        yield NoInternetConnection()

    def test_no_internet_connection_is_an_exception(self, exception):
        with pytest.raises(NoInternetConnection):
            raise exception

    def test_message_is_stored(self, exception, message):
        assert exception.message == message

    def test_default_message_is_stored(self, exception_with_default_message):
        message = "There is currently no internet connection."
        assert exception_with_default_message.message == message


class TestUnsupportedModelType:
    """Unit tests for the UnsupportedModelType exception class."""

    @pytest.fixture(scope="class")
    def model_type(self):
        yield "test_model_type"

    @pytest.fixture(scope="class")
    def exception(self, model_type):
        yield UnsupportedModelType(model_type=model_type)

    def test_unsupported_model_type_is_an_exception(self, exception):
        with pytest.raises(UnsupportedModelType):
            raise exception

    def test_model_type_is_stored(self, exception, model_type):
        assert exception.model_type == model_type

    def test_message_is_stored(self, exception, model_type):
        message = (
            f"Received an unsupported model type: {model_type}, "
            "supported types are `nn.Module` and `PretrainedModel`."
        )
        assert exception.message == message