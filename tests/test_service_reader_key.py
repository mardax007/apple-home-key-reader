import os

from entity import Issuer, KeyType, OperationStatus, ReaderKeyRequest
from repository import Repository
from service import Service


class FakeCLF:
    pass


def test_remove_reader_key_resets_existing_instance(tmp_path):
    storage_path = tmp_path / "homekey.json"
    repository = Repository(str(storage_path))
    service = Service(FakeCLF(), repository)

    private_key = os.urandom(32)
    reader_identifier = os.urandom(8)
    service.add_reader_key(
        ReaderKeyRequest(
            key_type=KeyType.SECP256R1,
            reader_private_key=private_key,
            unique_reader_identifier=reader_identifier,
        )
    )
    repository.upsert_issuer(Issuer(public_key=os.urandom(32), endpoints=[]))

    response = service.remove_reader_key(
        ReaderKeyRequest(key_identifier=repository.get_reader_group_identifier())
    )

    assert response.status == OperationStatus.SUCCESS
    assert repository.get_reader_private_key() == Service.UNCONFIGURED_READER_PRIVATE_KEY
    assert repository.get_reader_identifier() == Repository.UNCONFIGURED_READER_IDENTIFIER
    assert repository.get_all_issuers() == []


def test_remove_unknown_reader_key_returns_does_not_exist(tmp_path):
    storage_path = tmp_path / "homekey.json"
    repository = Repository(str(storage_path))
    service = Service(FakeCLF(), repository)

    private_key = os.urandom(32)
    reader_identifier = os.urandom(8)
    service.add_reader_key(
        ReaderKeyRequest(
            key_type=KeyType.SECP256R1,
            reader_private_key=private_key,
            unique_reader_identifier=reader_identifier,
        )
    )
    expected_private_key = repository.get_reader_private_key()
    expected_reader_identifier = repository.get_reader_identifier()
    expected_key_identifier = repository.get_reader_group_identifier()

    response = service.remove_reader_key(ReaderKeyRequest(key_identifier=os.urandom(8)))

    assert response.status == OperationStatus.DOES_NOT_EXIST
    assert repository.get_reader_private_key() == expected_private_key
    assert repository.get_reader_identifier() == expected_reader_identifier
    assert repository.get_reader_group_identifier() == expected_key_identifier
