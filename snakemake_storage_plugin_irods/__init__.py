from dataclasses import dataclass, field
import datetime
import json
import os
from pathlib import PosixPath
from typing import Any, Iterable, Optional, List
from urllib.parse import urlparse

from irods import password_obfuscation
from irods.session import iRODSSession
from irods.models import DataObject
from irods.exception import (
    CollectionDoesNotExist,
    DataObjectDoesNotExist,
    CAT_NO_ACCESS_PERMISSION,
    CAT_NAME_EXISTS_AS_DATAOBJ,
)
import irods.keywords as kw

from snakemake_interface_storage_plugins.settings import StorageProviderSettingsBase
from snakemake_interface_storage_plugins.storage_provider import (  # noqa: F401
    StorageProviderBase,
    StorageQueryValidationResult,
    ExampleQuery,
    Operation,
    QueryType,
)
from snakemake_interface_storage_plugins.storage_object import (
    StorageObjectRead,
    StorageObjectWrite,
    StorageObjectGlob,
    retry_decorator,
)
from snakemake_interface_storage_plugins.io import IOCacheStorageInterface


env_msg = "Value in ~/.irods/irods_environment.json has higher priority, if present. "
pswd_msg = (
    "If no password is provided in the settings or environment file, the"
    + " password in ~/.irods/.irodsA will be used with native authentication. "
)


@dataclass
class StorageProviderSettings(StorageProviderSettingsBase):
    host: Optional[str] = field(
        default=None,
        metadata={
            "help": f"The host name of the iRODS server. {env_msg}",
            "env_var": False,
            "required": True,
        },
    )
    port: Optional[int] = field(
        default=None,
        metadata={
            "help": f"The port of the iRODS server. {env_msg}",
            "env_var": False,
            "required": True,
        },
    )
    username: Optional[str] = field(
        default=None,
        metadata={
            "help": f"The user name for the iRODS server. {env_msg}",
            "env_var": True,
            "required": True,
        },
    )
    password: Optional[str] = field(
        default=None,
        metadata={
            "help": f"The password for the iRODS server. {env_msg} {pswd_msg}",
            "env_var": True,
            "required": False,
        },
    )
    zone: Optional[str] = field(
        default=None,
        metadata={
            "help": f"The zone for the iRODS server. {env_msg}",
            "env_var": False,
            "required": True,
        },
    )
    home: Optional[str] = field(
        default=None,
        metadata={
            "help": f"The home parameter for the iRODS server. {env_msg}",
            "env_var": False,
            "required": True,
        },
    )
    authentication_scheme: str = field(
        default="native",
        metadata={
            "help": "The authentication scheme for the iRODS server. "
            + f"{env_msg} {pswd_msg}",
            "env_var": False,
            "required": True,
        },
    )
    encryption_algorithm: str = field(
        default="AES-256-CBC",
        metadata={
            "help": f"Encryption algorithm for parallel transfer encryption. {env_msg}",
            "env_var": False,
            "required": False,
        },
    )
    encryption_key_size: int = field(
        default=32,
        metadata={
            "help": f"Key size for parallel transfer encryption. {env_msg}",
            "env_var": False,
            "required": False,
        },
    )
    encryption_num_hash_rounds: int = field(
        default=16,
        metadata={
            "help": "Number of hash rounds for parallel transfer "
            + f"encryption. {env_msg}",
            "env_var": False,
            "required": False,
        },
    )
    encryption_salt_size: int = field(
        default=8,
        metadata={
            "help": f"Salt size for parallel transfer encryption. {env_msg}",
            "env_var": False,
            "required": False,
        },
    )
    client_server_negotiation: Optional[str] = field(
        default=None,
        metadata={
            "help": f"Set to 'request_server_negotiation' to enable SSL/TLS. {env_msg}",
            "env_var": False,
            "required": False,
        },
    )
    client_server_policy: str = field(
        default="CS_NEG_REFUSE",
        metadata={
            "help": "CS_NEG_REFUSE: no SSL/TLS, CS_NEG_REQUIRE: enforce SSL/TLS, "
            + f"CS_NEG_DONT_CARE: let server decide. {env_msg}",
            "env_var": False,
            "required": False,
        },
    )
    ssl_verify_server: str = field(
        default="hostname",
        metadata={
            "help": "none: do not verify certificate, cert: verify certificate"
            + " validity (but not hostname), "
            + f"hostname: verify certificate validity and hostname. {env_msg}",
            "env_var": False,
            "required": False,
        },
    )
    ssl_ca_certificate_file: Optional[str] = field(
        default=None,
        metadata={
            "help": "Path to file with trusted CA certificates in PEM format. "
            + "Used in conjunction with system default trusted certificates."
            + f" {env_msg}",
            "env_var": False,
            "required": False,
        },
    )
    use_ssl: bool = field(
        default=False,
        metadata={
            "help": "Whether to use SSL when connecting to the server.",
            "env_var": False,
            "required": False,
        },
    )

    def __post_init__(self):
        env_file = PosixPath(os.path.expanduser("~/.irods/irods_environment.json"))
        if env_file.exists():
            with open(env_file) as f:
                env = json.load(f)

            def retrieve(src, trgt):
                if src in env:
                    setattr(self, trgt, env[src])

            retrieve("irods_host", "host")
            retrieve("irods_port", "port")
            retrieve("irods_user_name", "username")
            retrieve("irods_password", "password")
            retrieve("irods_zone_name", "zone")
            retrieve("irods_authentication_scheme", "authentication_scheme")
            retrieve("irods_home", "home")
            retrieve("irods_encryption_algorithm", "encryption_algorithm")
            retrieve("irods_encryption_key_size", "encryption_key_size")
            retrieve("irods_encryption_num_hash_rounds", "encryption_num_hash_rounds")
            retrieve("irods_encryption_salt_size", "encryption_salt_size")
            retrieve("irods_client_server_negotiation", "client_server_negotiation")
            retrieve("irods_client_server_policy", "client_server_policy")
            retrieve("irods_ssl_verify_server", "ssl_verify_server")
            retrieve("irods_ssl_ca_certificate_file", "ssl_ca_certificate_file")


utc = datetime.datetime.fromtimestamp(0, datetime.timezone.utc)


# Required:
# Implementation of your storage provider
# This class can be empty as the one below.
# You can however use it to store global information or maintain e.g. a connection
# pool.
class StorageProvider(StorageProviderBase):
    # For compatibility with future changes, you should not overwrite the __init__
    # method. Instead, use __post_init__ to set additional attributes and initialize
    # futher stuff.

    def __post_init__(self):
        # This is optional and can be removed if not needed.
        # Alternatively, you can e.g. prepare a connection to your storage backend here.
        # and set additional attributes.
        if self.settings.use_ssl:
            ssl_settings = {
                "client_server_negotiation": self.settings.client_server_negotiation,
                "client_server_policy": self.settings.client_server_policy,
                "encryption_algorithm": self.settings.encryption_algorithm,
                "encryption_key_size": self.settings.encryption_key_size,
                "encryption_num_hash_rounds": self.settings.encryption_num_hash_rounds,
                "encryption_salt_size": self.settings.encryption_salt_size,
                "ssl_verify_server": self.settings.ssl_verify_server,
                "ssl_ca_certificate_file": self.settings.ssl_ca_certificate_file,
            }
        else:
            ssl_settings = {}

        if self.settings.password is None:
            irodsA = os.path.expanduser("~/.irods/.irodsA")
            try:
                with open(irodsA, "r") as r:
                    scrambled_password = r.read()
                    password = password_obfuscation.decode(scrambled_password)
                    authentication_scheme = "native"
            except OSError as err:
                raise Exception(
                    "Error: could not retrieve irods_password from"
                    + " settings or ~/.irods/.irodsA file."
                ) from err
        else:
            password = self.settings.password
            authentication_scheme = self.settings.authentication_scheme

        self.session = iRODSSession(
            host=self.settings.host,
            port=self.settings.port,
            user=self.settings.username,
            password=password,
            zone=self.settings.zone,
            authentication_scheme=authentication_scheme,
            **ssl_settings,
        )

    @classmethod
    def example_queries(cls) -> List[ExampleQuery]:
        """Return an example query with description for this storage provider."""
        return [
            ExampleQuery(
                query="irods://folder/myfile.txt",
                type=QueryType.ANY,
                description="A file in a folder on the iRODS server.",
            ),
        ]

    def rate_limiter_key(self, query: str, operation: Operation) -> Any:
        """Return a key for identifying a rate limiter given a query and an operation.

        This is used to identify a rate limiter for the query.
        E.g. for a storage provider like http that would be the host name.
        For s3 it might be just the endpoint URL.
        """
        return self.settings.host

    def default_max_requests_per_second(self) -> float:
        """Return the default maximum number of requests per second for this storage
        provider."""
        return 10.0

    def use_rate_limiter(self) -> bool:
        """Return False if no rate limiting is needed for this provider."""
        return True

    @classmethod
    def is_valid_query(cls, query: str) -> StorageQueryValidationResult:
        """Return whether the given query is valid for this storage provider."""
        # Ensure that also queries containing wildcards (e.g. {sample}) are accepted
        # and considered valid. The wildcards will be resolved before the storage
        # object is actually used.
        parsed = urlparse(query)
        if parsed.scheme == "irods" and parsed.path:
            return StorageQueryValidationResult(valid=True, query=query)
        else:
            return StorageQueryValidationResult(
                valid=False,
                query=query,
                reason="Query does not start with irods:// or does not "
                "contain a path to a file or directory.",
            )


# Required:
# Implementation of storage object. If certain methods cannot be supported by your
# storage (e.g. because it is read-only see
# snakemake-storage-http for comparison), remove the corresponding base classes
# from the list of inherited items.
class StorageObject(StorageObjectRead, StorageObjectWrite, StorageObjectGlob):
    # For compatibility with future changes, you should not overwrite the __init__
    # method. Instead, use __post_init__ to set additional attributes and initialize
    # futher stuff.

    def __post_init__(self):
        # This is optional and can be removed if not needed.
        # Alternatively, you can e.g. prepare a connection to your storage backend here.
        # and set additional attributes.
        self.parsed_query = urlparse(self.query)
        self.path = PosixPath(
            f"/{self.parsed_query.netloc}"
        ) / self.parsed_query.path.lstrip("/")

    async def inventory(self, cache: IOCacheStorageInterface):
        """From this file, try to find as much existence and modification date
        information as possible. Only retrieve that information that comes for free
        given the current object.
        """
        # This is optional and can be left as is

        # If this is implemented in a storage object, results have to be stored in
        # the given IOCache object, using self.cache_key() as key.
        # Optionally, this can take a custom local suffix, needed e.g. when you want
        # to cache more items than the current query: self.cache_key(local_suffix=...)
        pass

    def get_inventory_parent(self) -> Optional[str]:
        """Return the parent directory of this object."""
        # this is optional and can be left as is
        return None

    def local_suffix(self) -> str:
        """Return a unique suffix for the local path, determined from self.query."""
        return str(self.path).lstrip("/")

    def cleanup(self):
        """Perform local cleanup of any remainders of the storage object."""
        # self.local_path() should not be removed, as this is taken care of by
        # Snakemake.
        pass

    # Fallible methods should implement some retry logic.
    # The easiest way to do this (but not the only one) is to use the retry_decorator
    # provided by snakemake-interface-storage-plugins.
    @retry_decorator
    def exists(self) -> bool:
        # TODO does this also work for collections?
        # return True if the object exists
        try:
            self._data_obj()
            return True
        except (CollectionDoesNotExist, DataObjectDoesNotExist):
            return False

    def _data_obj(self):
        return self.provider.session.data_objects.get(str(self.path))

    @retry_decorator
    def mtime(self) -> float:
        # TODO does this also work for collections (i.e. directories)?
        # return the modification time
        meta = self.provider.session.metadata.get(DataObject, str(self.path))
        for m in meta:
            if m.name == "mtime":
                return float(m.value)
        # TODO is this conversion needed? Unix timestamp is always UTC, right?
        # dt = self._convert_time(self._data_obj().modify_time, timezone)
        return self._data_obj().modify_time.timestamp()

    @retry_decorator
    def size(self) -> int:
        # return the size in bytes
        return self._data_obj().size

    @retry_decorator
    def retrieve_object(self):
        # Ensure that the object is accessible locally under self.local_path()
        opts = {kw.FORCE_FLAG_KW: ""}
        try:
            # is directory
            collection = self.provider.session.collections.get(str(self.path))
            for _, _, objs in collection.walk():
                for obj in objs:
                    self.provoder.session.data_objects.get(
                        obj.path, str(self.local_path() / obj.path), options=opts
                    )
        except CollectionDoesNotExist:
            # is file
            self.provider.session.data_objects.get(
                str(self.path), str(self.local_path()), options=opts
            )

    # The following to methods are only required if the class inherits from
    # StorageObjectReadWrite.

    @retry_decorator
    def store_object(self):
        # Ensure that the object is stored at the location specified by
        # self.local_path().
        def mkdir(path):
            try:
                self.provider.session.collections.get(path)
            except CAT_NO_ACCESS_PERMISSION:
                pass
            except CollectionDoesNotExist:
                self.provider.session.collections.create(path)

        for parent in self.path.parents[:-2][::-1]:
            mkdir(str(parent))

        if self.local_path().is_dir():
            mkdir(str(self.path))
            for f in self.local_path().iterdir():
                self.provider.session.data_objects.put(str(f), str(self.path / f.name))
        else:
            self.provider.session.data_objects.put(
                str(self.local_path()), str(self.path)
            )

    @retry_decorator
    def remove(self):
        # Remove the object from the storage.
        try:
            self.provider.session.collections.unregister(str(self.path))
        except CAT_NAME_EXISTS_AS_DATAOBJ:
            self.provider.session.data_objects.unregister(str(self.path))

    # The following to methods are only required if the class inherits from
    # StorageObjectGlob.

    @retry_decorator
    def list_candidate_matches(self) -> Iterable[str]:
        """Return a list of candidate matches in the storage for the query."""
        # This is used by glob_wildcards() to find matches for wildcards in the query.
        # The method has to return concretized queries without any remaining wildcards.
        # Use snakemake_executor_plugins.io.get_constant_prefix(self.query) to get the
        # prefix of the query before the first wildcard.
        ...

    def _convert_time(self, timestamp, tz=None):
        dt = timestamp.replace(tzinfo=datetime.timezone("UTC"))
        if tz:
            dt = dt.astimezone(datetime.timezone(tz))
        return dt
