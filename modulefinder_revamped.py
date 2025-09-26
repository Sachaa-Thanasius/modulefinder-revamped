from __future__ import annotations

import collections.abc as _cabc
import contextlib
import dis
import importlib.util
import os
import sys
import threading
import types
from importlib._bootstrap import (
    _calc___package__,  # pyright: ignore[reportUnknownVariableType, reportAttributeAccessIssue]
)
from importlib.machinery import BuiltinImporter, FrozenImporter, ModuleSpec, PathFinder


TYPE_CHECKING = False

if TYPE_CHECKING:
    import typing as _t

    from _typeshed.importlib import MetaPathFinderProtocol as _MetaPathFinderProtocol

    _OpcodeInfo: _t.TypeAlias = (
        tuple[_t.Literal["store"], tuple[str]] | tuple[_t.Literal["import"], tuple[str, list[str] | None, int]]
    )
else:
    _MetaPathFinderProtocol = _OpcodeInfo = object


# Used for a parameter annotation.
if TYPE_CHECKING:
    import importlib.abc  # noqa: TC004


# Used to tell type-checkers that dis has these private functions with specific signatures.
if TYPE_CHECKING:
    import typing as _t

    @_t.runtime_checkable
    class _DisModule(_t.Protocol):
        def _find_store_names(self, co: types.CodeType) -> _cabc.Generator[str]: ...
        def _find_imports(self, co: types.CodeType) -> _cabc.Generator[tuple[str, int, list[str] | None]]: ...

    assert isinstance(dis, _DisModule)


_StrPath = str | os.PathLike[str]

_MISSING = object()


@contextlib.contextmanager
def _patch_attr(obj: object, attr_name: str, new_value: object, /) -> _cabc.Generator[None]:
    """Context manager for temporarily patching the attribute of an object."""

    old_value = _MISSING
    try:
        old_value = getattr(obj, attr_name)
        setattr(obj, attr_name, new_value)
        yield
    finally:
        if old_value is not _MISSING:
            setattr(obj, attr_name, old_value)


def _replace_paths_in_code(code: types.CodeType, path_replacements: list[tuple[str, str]]) -> types.CodeType:
    original_filename = os.path.normpath(code.co_filename)

    for old_path, new_path in path_replacements:
        if original_filename.startswith(old_path):
            new_filename = original_filename.replace(old_path, new_path, 1)
            break
    else:
        new_filename = original_filename

    new_consts = list(code.co_consts)
    for i, const in enumerate(new_consts):
        if isinstance(const, types.CodeType):
            new_consts[i] = _replace_paths_in_code(const, path_replacements)

    return code.replace(co_consts=tuple(new_consts), co_filename=new_filename)


def _scan_opcodes(code: types.CodeType) -> _cabc.Generator[_OpcodeInfo]:
    """Scan the code, and yield 'interesting' opcode combinations."""

    # These private dis functions exist specifically for modulefinder's use.
    for name in dis._find_store_names(code):  # pyright: ignore [reportPrivateUsage]
        yield ("store", (name,))
    for name, level, fromlist in dis._find_imports(code):  # pyright: ignore [reportPrivateUsage]
        yield ("import", (name, fromlist, level))


def _scan_code(mf: ModuleFinder, module: MFModuleType, code: types.CodeType) -> None:  # noqa: PLR0912
    for opcode_info in _scan_opcodes(code):
        match opcode_info:
            case ("store", (name,)):
                module.__mf_global_names__.add(name)

            case ("import", (name, fromlist, level)):
                if fromlist is not None:
                    have_star = "*" in fromlist
                    fromlist = [f for f in fromlist if f != "*"]
                else:
                    have_star = False
                    fromlist = []

                try:
                    if level > 0:
                        package: str | None = _calc___package__(module.__dict__)  # pyright: ignore[reportUnknownVariableType]
                        assert isinstance(package, str) or (package is None)
                        name = importlib.util.resolve_name("." * level + name, package)
                        level = 0

                    # NOTE: We use importlib.__import__ to avoid special-casing of builtin modules like sys.
                    result_module = importlib.__import__(name, module.__dict__, module.__dict__, fromlist, level)
                except (ImportError, SyntaxError):
                    mf.bad_modules.setdefault(name, set()).add(module.__name__)
                else:
                    if hasattr(result_module, "__path__"):
                        for from_item in fromlist:
                            if not hasattr(result_module, from_item):
                                mf.bad_modules.setdefault(f"{name}.{from_item}", set()).add(module.__name__)

                if have_star:
                    # We've encountered an "import *". If it is a Python module,
                    # the code has already been parsed and we can suck out the
                    # global names.
                    if (cached_mod := sys.modules.get(name)) is not None:
                        assert isinstance(cached_mod, MFModuleType)
                        module.__mf_global_names__ |= cached_mod.__mf_global_names__
                        module.__mf_star_imports__ |= cached_mod.__mf_star_imports__
                        if cached_mod.__code__ is None:
                            module.__mf_star_imports__.add(name)
                    else:
                        module.__mf_star_imports__.add(name)

            case unknown:  # pyright: ignore [reportUnnecessaryComparison] # Get a good error message.
                msg = f"Unknown opcode info: {unknown!r}"
                raise RuntimeError(msg)

    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            _scan_code(mf, module, const)


def _inject_mf_into_spec(spec: ModuleSpec, mf: ModuleFinder) -> ModuleSpec:
    if (loader := spec.loader) is not None:
        spec.loader = _MFLoader(loader)  # pyright: ignore[reportAttributeAccessIssue, reportArgumentType]
        spec.loader_state = {"mf": mf}
    return spec


class MFModuleType(types.ModuleType):
    """Our stand-in for `types.ModuleType`.

    Attributes
    ----------
    __code__: types.CodeType | None
        The unexecuted module code.
    global_names: set[str]
        The set of global names that are assigned to within the module. This includes those names imported through
        star-imports of Python modules.
    star_imports: set[str]
        The set of star-imports this module did that could not be resolved, ie. a star-import from a non-Python module.
    """

    __code__: types.CodeType | None
    __mf_global_names__: set[str]
    __mf_star_imports__: set[str]


class _MFLoader:
    def __init__(self, loader: importlib.abc.InspectLoader) -> None:
        self.loader = loader

    def create_module(self, spec: ModuleSpec) -> MFModuleType:
        return MFModuleType(spec.name)

    def exec_module(self, module: types.ModuleType) -> None:
        assert isinstance(module, MFModuleType)

        # Initialize mf-specific module attributes with default values.
        module.__code__ = None
        module.__mf_global_names__ = set()
        module.__mf_star_imports__ = set()

        if (code := self.loader.get_code(module.__name__)) is not None:
            spec = module.__spec__
            assert spec is not None
            mf: ModuleFinder = spec.loader_state["mf"]
            spec.loader_state = None

            if mf.path_replacements:
                code = _replace_paths_in_code(code, mf.path_replacements)
            module.__code__ = code

            _scan_code(mf, module, code)


class _MFFinder:
    def __init__(self, mf: ModuleFinder, finder: _MetaPathFinderProtocol) -> None:
        self.mf = mf
        self.finder = finder

    def find_spec(
        self,
        fullname: str,
        path: _cabc.Sequence[str] | None = None,
        target: types.ModuleType | None = None,
        /,
    ) -> ModuleSpec | None:
        invalidate_caches = getattr(self.finder, "invalidate_caches", None)
        if callable(invalidate_caches):
            invalidate_caches()

        spec = self.finder.find_spec(fullname, path, target)
        if spec is not None:
            spec = _inject_mf_into_spec(spec, self.mf)

        return spec


class ModuleFinder:
    """A class that tracks module imports recursively.

    Attributes
    ----------
    path: list[_StrPath]
        A stand-in for `sys.path`. Defaults to `sys.path`.
    path_replacements: list[tuple[_StrPath, _StrPath]]
        A list of (oldpath, newpath) tuples that will be replaced in module paths.
    modules: dict[str, MFModuleType]
        A stand-in for `sys.modules`.
    meta_path: list[_MetaPathFinderProtocol]
        A stand-in for `sys.meta_path`.
    """

    #: A threading lock to guard the patching of sys that ModuleFinder does.
    __sys_patch_lock = threading.Lock()

    def __init__(
        self,
        path: _cabc.Sequence[_StrPath] | None = None,
        path_replacements: _cabc.Sequence[tuple[_StrPath, _StrPath]] | None = None,
        excludes: _cabc.Sequence[str] | None = None,
    ) -> None:
        if path_replacements is not None:
            self.path_replacements = [(os.fspath(old), os.fspath(new)) for old, new in path_replacements]
        else:
            self.path_replacements = []
        self.excludes: list[str] = list(excludes) if (excludes is not None) else []  # TODO

        self.path: list[str] = [os.fspath(p) for p in path] if (path is not None) else sys.path
        self.modules: dict[str, MFModuleType] = {}
        self.meta_path: list[_MetaPathFinderProtocol] = [
            _MFFinder(self, BuiltinImporter),
            _MFFinder(self, FrozenImporter),
            _MFFinder(self, PathFinder),
        ]

        self.bad_modules: dict[str, set[str]] = {}

    @contextlib.contextmanager
    def _patch_sys(self) -> _cabc.Generator[None]:
        with (
            self.__sys_patch_lock,
            _patch_attr(sys, "modules", self.modules),
            _patch_attr(sys, "path", self.path),
            _patch_attr(sys, "meta_path", self.meta_path),
            _patch_attr(sys, "path_hooks", list(sys.path_hooks)),
            _patch_attr(sys, "path_importer_cache", dict(sys.path_importer_cache)),
        ):
            yield

    def _import_from_file(self, name: str, pathname: _StrPath) -> None:
        with self._patch_sys():
            spec = importlib.util.spec_from_file_location(name, pathname)
            if spec is None:
                raise FileNotFoundError(pathname)
            spec = _inject_mf_into_spec(spec, self)
            assert spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            assert isinstance(module, MFModuleType)
            assert module.__code__ is not None
            sys.modules[name] = module
            spec.loader.exec_module(module)

    def import_as_module(
        self,
        name: str,
        caller: MFModuleType | None = None,
        fromlist: _cabc.Sequence[str] | None = (None),
        level: int = 0,
    ) -> None:
        globals_ = caller.__dict__ if (caller is not None) else None
        with self._patch_sys():
            # NOTE: We use importlib.__import__ to avoid special-casing of builtin modules like sys.
            importlib.__import__(name, globals_, globals_, fromlist, level)  # pyright: ignore[reportArgumentType]

    def import_as_file(self, pathname: _StrPath) -> None:
        _dir, tail = os.path.split(pathname)
        name, _ext = os.path.splitext(tail)
        self._import_from_file(name, pathname)

    def run_as_script(self, pathname: _StrPath) -> None:
        name = "__main__"
        self._import_from_file(name, pathname)

    def any_missing_maybe(self) -> tuple[list[str], list[str]]:
        """Return two lists, one with modules that are certainly missing
        and one with modules that *may* be missing. The latter names could
        either be submodules *or* just global names in the package.

        The reason it can't always be determined is that it's impossible to
        tell which names are imported when "from module import *" is done
        with an extension module, short of actually importing it.
        """

        missing: list[str] = []
        maybe: list[str] = []

        for name in self.bad_modules:
            if name in self.excludes:
                continue

            if "." not in name:
                missing.append(name)
                continue

            pkgname, _, subname = name.rpartition(".")
            pkg = self.modules.get(pkgname)

            if pkg is not None:
                if pkgname in self.bad_modules[name]:
                    # The package tried to import this module itself and
                    # failed. It's definitely missing.
                    missing.append(name)
                elif subname in pkg.__mf_global_names__:
                    # It's a global in the package: definitely not missing.
                    pass
                elif pkg.__mf_star_imports__:
                    # It could be missing, but the package did an "import *"
                    # from a non-Python module, so we simply can't be sure.
                    maybe.append(name)
                else:
                    # It's not a global in the package, the package didn't
                    # do funny star imports, it's very likely to be missing.
                    # The symbol could be inserted into the package from the
                    # outside, but since that's not good style we simply list
                    # it missing.
                    missing.append(name)
            else:
                missing.append(name)

        missing.sort()
        maybe.sort()
        return missing, maybe

    def report(self) -> None:  # pragma: no cover
        """Print a report.

        The report lists the found modules with their paths, as well as modules that are missing or seem to be missing.
        """

        print()
        print(f"  {'Name':25} File")
        print(f"  {'----':25} ----")

        # Print modules found
        keys = sorted(self.modules)
        for key in keys:
            m = self.modules[key]
            pkg_or_module = "P" if m.__path__ else "m"
            print(pkg_or_module, key.ljust(25), m.__file__ or "")

        missing, maybe = self.any_missing_maybe()

        def print_mods(mods: list[str]) -> None:
            for name in mods:
                mods = sorted(self.bad_modules[name])
                print("?", name, "imported from", ", ".join(mods))

        # Print missing modules
        if missing:
            print()
            print("Missing modules:")
            print_mods(missing)

        # Print modules that may be missing, but then again, maybe not...
        if maybe:
            print()
            print("Submodules that appear to be missing, but could also be global names in the parent package:")
            print_mods(maybe)
