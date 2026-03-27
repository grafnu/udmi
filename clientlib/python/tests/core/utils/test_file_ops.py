"""Tests for file_ops utility."""
from udmi.core.utils.file_ops import mask_secrets


def test_mask_secrets_default_keys():
    """Test masking with default sensitive keys."""
    data = {
        "username": "user123",
        "password": "secretpassword",
        "secret": "mysecret",
        "key_data": "somekeydata",
        "private_key": "privatekeycontent",
        "token": "mytoken",
        "auth": "authdata",
        "other": "not_sensitive"
    }
    expected = {
        "username": "user123",
        "password": "********",
        "secret": "********",
        "key_data": "********",
        "private_key": "********",
        "token": "********",
        "auth": "********",
        "other": "not_sensitive"
    }
    assert mask_secrets(data) == expected


def test_mask_secrets_custom_keys():
    """Test masking with custom sensitive keys."""
    data = {
        "apiKey": "12345",
        "id": "abc"
    }
    # Key name must contain the sensitive term as a substring
    sensitive_keys = ["api"]
    expected = {
        "apiKey": "********",
        "id": "abc"
    }
    assert mask_secrets(data, sensitive_keys) == expected


def test_mask_secrets_custom_keys_uppercase():
    """Test masking with custom uppercase sensitive keys."""
    data = {
        "apiKey": "12345",
        "id": "abc"
    }
    sensitive_keys = ["API"] # Should be case-insensitive
    expected = {
        "apiKey": "********",
        "id": "abc"
    }
    assert mask_secrets(data, sensitive_keys) == expected


def test_mask_secrets_nested_dict():
    """Test recursive masking in nested dictionaries."""
    data = {
        "metadata": {
            "token": "nested_token",
            "info": "nested_info"
        },
        "status": "ok"
    }
    expected = {
        "metadata": {
            "token": "********",
            "info": "nested_info"
        },
        "status": "ok"
    }
    assert mask_secrets(data) == expected


def test_mask_secrets_nested_list():
    """Test recursive masking in nested lists."""
    data = [
        {"password": "pw1", "user": "u1"},
        {"password": "pw2", "user": "u2"},
        "some_string",
        123
    ]
    expected = [
        {"password": "********", "user": "u1"},
        {"password": "********", "user": "u2"},
        "some_string",
        123
    ]
    assert mask_secrets(data) == expected


def test_mask_secrets_case_insensitive_substring():
    """Test case-insensitivity and substring matching."""
    data = {
        "My_Password_Field": "pw123",
        "SECRET_TOKEN": "token123",
        "authentication": "auth123"
    }
    # "password" is in "My_Password_Field"
    # "secret" is in "SECRET_TOKEN"
    # "auth" is in "authentication"
    expected = {
        "My_Password_Field": "********",
        "SECRET_TOKEN": "********",
        "authentication": "********"
    }
    assert mask_secrets(data) == expected


def test_mask_secrets_primitive_types():
    """Test handling of primitive types."""
    assert mask_secrets("string") == "string"
    assert mask_secrets(123) == 123
    assert mask_secrets(True) is True
    assert mask_secrets(None) is None


def test_mask_secrets_empty():
    """Test handling of empty data."""
    assert mask_secrets({}) == {}
    assert mask_secrets([]) == []


def test_mask_secrets_deeply_nested():
    """Test deeply nested structures."""
    data = {
        "level1": {
            "level2": [
                {"secret": "deep_secret"}
            ]
        }
    }
    expected = {
        "level1": {
            "level2": [
                {"secret": "********"}
            ]
        }
    }
    assert mask_secrets(data) == expected
