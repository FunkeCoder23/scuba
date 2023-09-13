from __future__ import annotations
import dataclasses
import os
from pathlib import Path
import re
import shlex
from typing import Any, List, Dict, Optional, TextIO, Tuple, Type, TypeVar, Union
from typing import overload

import yaml
import yaml.nodes

from .constants import *
from .utils import *
from .dockerutil import make_vol_opt

CfgNode = Any
CfgData = Dict[str, CfgNode]
Environment = Dict[str, str]
_T = TypeVar("_T")


class ConfigError(Exception):
    pass


class ConfigNotFoundError(ConfigError):
    pass


class OverrideMixin:
    """
    A mixin class that indicates an instance's value should override something

    This class is mixed into objects loaded from YAML with an !override tag,
    and any object can be checked if it is an OverrideMixin using isinstance().
    """


class OverrideNone(OverrideMixin):
    """
    Represents a None value that also has Override behavior
    """

    def __bool__(self) -> bool:
        return False


class OverrideList(list, OverrideMixin):
    pass


class OverrideStr(str, OverrideMixin):
    pass


# http://stackoverflow.com/a/9577670
class Loader(yaml.SafeLoader):
    _root: Path  # directory containing the loaded document
    _cache: Dict[Path, Any]  # document path => document

    def __init__(self, stream: TextIO):
        if not hasattr(self, "_root"):
            self._root = Path(stream.name).parent
        self._cache = dict()
        super().__init__(stream)

    @staticmethod
    def _rooted_loader(root: Path) -> Type[Loader]:
        """Get a Loader class with _root set to root"""

        class RootedLoader(Loader):
            pass

        RootedLoader._root = root
        return RootedLoader

    def from_yaml(self, node: yaml.nodes.Node) -> Any:
        """
        Implementes a !from_yaml constructor with the following syntax:
            !from_yaml filename key

        Arguments:
            filename:   Filename of external YAML document from which to load,
                        relative to the current YAML file.
            key:        Key from external YAML document to return,
                        using a dot-separated syntax for nested keys.

        Examples:
            !from_yaml external.yml pop
            !from_yaml external.yml foo.bar.pop
            !from_yaml "another file.yml" "foo bar.snap crackle.pop"
        """

        # Load the content from the node, as a scalar
        assert isinstance(node, yaml.nodes.ScalarNode)
        content = self.construct_scalar(node)
        assert isinstance(content, str)

        # Split on unquoted spaces
        parts = shlex.split(content)
        if len(parts) != 2:
            raise yaml.YAMLError("Two arguments expected to !from_yaml")
        filename, key = parts

        # path is relative to the current YAML document
        path = self._root / filename

        # Load the other YAML document
        doc = self._cache.get(path)
        if not doc:
            with path.open("r") as f:
                doc = yaml.load(f, self.__class__)
                self._cache[path] = doc

        # Retrieve the key
        try:
            cur = doc
            # Use a negative look-behind to split the key on non-escaped '.' characters
            for k in re.split(r"(?<!\\)\.", key):
                cur = cur[
                    k.replace("\\.", ".")
                ]  # Be sure to replace any escaped '.' characters with *just* the '.'
        except KeyError:
            raise yaml.YAMLError(f"Key {key!r} not found in {filename}")
        return cur

    def override(self, node: yaml.nodes.Node) -> OverrideMixin:
        """
        Implements !override constructor
        """
        # Load the content from the node, as a scalar
        assert isinstance(node, yaml.nodes.ScalarNode)
        content = self.construct_scalar(node)
        assert isinstance(content, str)

        # Dynamically add an OverrideMixin to the resulting object's type
        obj = yaml.load(content, self._rooted_loader(root=self._root))
        if obj is None:
            obj = OverrideNone()
        else:
            objtype = type(obj)
            mixin_type = type(
                "Override" + objtype.__name__, (objtype, OverrideMixin), dict()
            )

            try:
                obj.__class__ = mixin_type
            except TypeError:
                # Primitive classes (e.g., int, str) don't support __class__ assignment
                obj = mixin_type(obj)

        assert isinstance(obj, OverrideMixin)
        return obj


Loader.add_constructor("!from_yaml", Loader.from_yaml)
Loader.add_constructor("!override", Loader.override)


def find_config() -> Tuple[Path, Path, ScubaConfig]:
    """Search up the directory hierarchy for .scuba.yml

    Returns: path, rel, config on success, or None if not found
        path    The absolute path of the directory where .scuba.yml was found
        rel     The relative path from the directory where .scuba.yml was found
                to the current directory
        config  The loaded configuration
    """
    cross_fs = "SCUBA_DISCOVERY_ACROSS_FILESYSTEM" in os.environ
    path = Path.cwd()

    while True:
        cfg_path = path / SCUBA_YML
        if cfg_path.exists():
            return path, Path.cwd().relative_to(path), load_config(cfg_path, path)

        if not cross_fs and path.is_mount():
            raise ConfigNotFoundError(
                f"{SCUBA_YML} not found here or any parent up to mount point {path}"
                "\nStopping at filesystem boundary"
                " (SCUBA_DISCOVERY_ACROSS_FILESYSTEM not set)."
            )

        # Traverse up directory hierarchy
        path, rest = path.parent, path.name
        if not rest:
            raise ConfigNotFoundError(
                f"{SCUBA_YML} not found here or any parent directories"
            )


def _process_script_node(node: CfgNode, name: str) -> List[str]:
    """Process a script-type node

    Args:
      node: A node of data retrieved from a YAML document. Should be a "common
        script schema" node as described in docs/configuration.rst.
      name: The name of the node.

    Returns:
      A script; a list of command strings.
    """
    if isinstance(node, str):
        # The script is just the text itself
        return [node]

    if isinstance(node, dict):
        # There must be a "script" key, which must be a list of strings
        script = node.get("script")
        if not script:
            raise ConfigError(f"{name}: must have a 'script' subkey")

        if isinstance(script, list):
            return script

        if isinstance(script, str):
            return [script]

        raise ConfigError(f"{name}.script: must be a string or list")

    raise ConfigError(f"{name}: must be string or dict")


def _process_environment(node: CfgNode, name: str) -> Environment:
    # Environment can be either a list of strings ("KEY=VALUE") or a mapping
    # Environment keys and values are always strings
    result = {}

    if not node:
        pass
    elif isinstance(node, dict):
        for k, v in node.items():
            if v is None:
                v = os.getenv(k, "")
            result[k] = str(v)
    elif isinstance(node, list):
        for e in node:
            k, v = parse_env_var(e)
            result[k] = v
    else:
        raise ConfigError(
            f"'{name}' must be list or mapping, not {type(node).__name__}"
        )

    return result


def _get_nullable_str(data: Dict[str, Any], key: str) -> Optional[str]:
    # N.B. We can't use data.get() here, because that might return
    # None, leading to ambiguity between the key being absent or set
    # to a null value.
    #
    # "Note that a null is different from an empty string and that a
    # mapping entry with some key and a null value is valid and
    # different from not having that key in the mapping."
    #   - http://yaml.org/type/null.html
    if not key in data:
        return None

    value = data[key]

    # We represent a null value as an empty string.
    if isinstance(value, OverrideNone):
        value = OverrideStr("")
    elif value is None:
        value = ""

    if not isinstance(value, str):
        raise ConfigError(f"{key!r} must be a string, not {type(value).__name__}")
    return value


def _get_entrypoint(data: CfgData) -> Optional[str]:
    return _get_nullable_str(data, "entrypoint")


def _get_docker_args(data: CfgData) -> Optional[List[str]]:
    args_str = _get_nullable_str(data, "docker_args")
    if args_str is None:
        return None

    override = isinstance(args_str, OverrideMixin)
    args = shlex.split(args_str)
    if override:
        args = OverrideList(args)

    return args


def _get_typed_val(
    data: CfgData,
    key: str,
    type_: Type[_T],
    default: Optional[_T] = None,
) -> Optional[_T]:
    v = data.get(key, default)
    if v is not None and not isinstance(v, type_):
        raise ConfigError(f"{key!r} must be a {type_.__name__}, not {type(v).__name__}")
    return v


@overload  # When default is None, can return None (Optional).
def _get_str(data: CfgData, key: str, default: None = None) -> Optional[str]:
    ...


@overload  # When default is non-None, cannot return None.
def _get_str(data: CfgData, key: str, default: str) -> str:
    ...


def _get_str(data: CfgData, key: str, default: Optional[str] = None) -> Optional[str]:
    return _get_typed_val(data, key, str, default)


def _get_dict(data: CfgData, key: str) -> Optional[dict[str, Any]]:
    return _get_typed_val(data, key, dict)


def _get_delimited_str_list(data: CfgData, key: str, sep: str) -> List[str]:
    s = _get_typed_val(data, key, str)
    return s.split(sep) if s else []


def _get_volumes(
    data: CfgData, scuba_root: Optional[Path]
) -> Optional[Dict[Path, ScubaVolume]]:
    voldata = _get_dict(data, "volumes")
    if voldata is None:
        return None

    vols = {}
    for cpath_str, v in voldata.items():
        cpath = _expand_path(cpath_str)  # no base_dir; container path must be absolute.
        vols[cpath] = ScubaVolume.from_dict(cpath, v, scuba_root)
    return vols


def _expand_path(in_str: str, base_dir: Optional[Path] = None) -> Path:
    """Expand variables in a path string and make it absolute.

    Environment variable references (e.g. $FOO and ${FOO}) are expanded using
    the host environment.

    After environment variable expansion, absolute paths are returned as-is.
    Relative paths are joined to base_dir, if provided.

    Args:
      in_str: Input path as a string.
      base_dir: Path to which relative paths will be joined.

    Returns:
      An absolute Path.

    Raises:
      ValueError: If base_dir is provided but not absolute.
      ConfigError: If a referenced environment variable is not set.
      ConfigError: An environment variable reference could not be parsed.
      ConfigError: A relative path is given when base_dir is not provided.
    """
    if base_dir is not None and not base_dir.is_absolute():
        raise ValueError(f"base_dir is not absolute: {base_dir}")

    try:
        output = expand_env_vars(in_str)
    except KeyError as ke:
        # pylint: disable=raise-missing-from
        raise ConfigError(
            f"Unset environment variable {ke.args[0]!r} used in {in_str!r}"
        )
    except ValueError as ve:
        raise ConfigError(
            f"Unable to expand string '{in_str}' due to parsing errors"
        ) from ve

    path = Path(output)

    if not path.is_absolute():
        if base_dir is None:
            raise ConfigError(f"Relative path not allowed: {path}")
        path = base_dir / path

    assert path.is_absolute()
    return path


@dataclasses.dataclass(frozen=True)
class ScubaVolume:
    container_path: Path
    host_path: Path  # TODO: Optional for anonymous volume
    options: List[str] = dataclasses.field(default_factory=list)

    @classmethod
    def from_dict(
        cls, cpath: Path, node: CfgNode, scuba_root: Optional[Path]
    ) -> ScubaVolume:
        # Treat a null node as an empty dict
        if node is None:
            node = {}

        # Simple form:
        # volumes:
        #   /foo: /host/foo
        if isinstance(node, str):
            return cls(
                container_path=cpath,
                host_path=_expand_path(node, scuba_root),
            )

        # Complex form
        # volumes:
        #   /foo:
        #     hostpath: /host/foo
        #     options: ro,z
        if isinstance(node, dict):
            hpath = node.get("hostpath")
            if hpath is None:
                raise ConfigError(f"Volume {cpath} must have a 'hostpath' subkey")
            return cls(
                container_path=cpath,
                host_path=_expand_path(hpath, scuba_root),
                options=_get_delimited_str_list(node, "options", ","),
            )

        raise ConfigError(f"{cpath}: must be string or dict")

    def get_vol_opt(self) -> str:
        return make_vol_opt(self.host_path, self.container_path, self.options)


@dataclasses.dataclass(frozen=True)
class ScubaAlias:
    name: str
    script: List[str]
    image: Optional[str] = None
    entrypoint: Optional[str] = None
    environment: Optional[Dict[str, str]] = None
    shell: Optional[str] = None
    as_root: bool = False
    docker_args: Optional[List[str]] = None
    volumes: Optional[Dict[Path, ScubaVolume]] = None

    @classmethod
    def from_dict(
        cls, name: str, node: CfgNode, scuba_root: Optional[Path]
    ) -> ScubaAlias:
        script = _process_script_node(node, name)

        if isinstance(node, dict):  # Rich alias
            return cls(
                name=name,
                script=script,
                image=node.get("image"),
                entrypoint=_get_entrypoint(node),
                environment=_process_environment(
                    node.get("environment"), f"{name}.environment"
                ),
                shell=node.get("shell"),
                as_root=bool(node.get("root")),
                docker_args=_get_docker_args(node),
                volumes=_get_volumes(node, scuba_root),
            )

        return cls(name=name, script=script)


class ScubaConfig:
    shell: str
    entrypoint: Optional[str]
    docker_args: Optional[List[str]]  # TODO: drop Optional?
    volumes: Optional[Dict[Path, ScubaVolume]]  # TODO: drop Optional? Dict?
    aliases: Dict[str, ScubaAlias]
    hooks: Dict[str, List[str]]
    environment: Environment

    def __init__(
        self,
        data: Optional[dict[str, CfgNode]] = None,
        scuba_root: Optional[Path] = None,
    ) -> None:
        if data is None:
            data = {}

        optional_nodes = (
            "image",
            "aliases",
            "hooks",
            "entrypoint",
            "environment",
            "shell",
            "docker_args",
            "volumes",
        )

        # Check for unrecognized nodes
        extra = [n for n in data if not n in optional_nodes]
        if extra:
            raise ConfigError(
                f"{SCUBA_YML}: Unrecognized node{'s' if len(extra) > 1 else ''}:"
                + ", ".join(extra)
            )

        self._image = _get_str(data, "image")
        self.shell = _get_str(data, "shell", DEFAULT_SHELL)
        self.entrypoint = _get_entrypoint(data)
        self.docker_args = _get_docker_args(data)
        self.volumes = _get_volumes(data, scuba_root)
        self.aliases = self._load_aliases(data, scuba_root)
        self.hooks = self._load_hooks(data)
        self.environment = _process_environment(data.get("environment"), "environment")

    def _load_aliases(
        self, data: CfgData, scuba_root: Optional[Path]
    ) -> Dict[str, ScubaAlias]:
        aliases = {}
        for name, node in data.get("aliases", {}).items():
            if " " in name:
                raise ConfigError("Alias names cannot contain spaces")
            aliases[name] = ScubaAlias.from_dict(name, node, scuba_root)
        return aliases

    def _load_hooks(self, data: CfgData) -> Dict[str, List[str]]:
        hooks = {}
        for name in (
            "user",
            "root",
        ):
            node = data.get("hooks", {}).get(name)
            if node:
                hooks[name] = _process_script_node(node, name)
        return hooks

    @property
    def image(self) -> str:
        if self._image is None:
            raise ConfigError("Top-level 'image' not set")
        return self._image


def load_config(path: Path, scuba_root: Path) -> ScubaConfig:
    try:
        with path.open("r") as f:
            data = yaml.load(f, Loader)
    except IOError as e:
        raise ConfigError(f"Error opening {SCUBA_YML}: {e}")
    except yaml.YAMLError as e:
        raise ConfigError(f"Error loading {SCUBA_YML}: {e}")

    return ScubaConfig(data, scuba_root)
