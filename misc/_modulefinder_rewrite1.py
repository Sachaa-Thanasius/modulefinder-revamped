from __future__ import annotations

import dis
import importlib._bootstrap
import importlib.machinery
import importlib.util
import os
import sys
import types
import collections.abc as _cabc


TYPE_CHECKING = False

if TYPE_CHECKING:
    import importlib.abc  # Used in a string for casting to InspectLoader.
    import typing as _t
    from _typeshed.importlib import MetaPathFinderProtocol as _MetaPathFinderProtocol

    type _OpcodeInfo = (
        tuple[_t.Literal["store"], tuple[str]] | tuple[_t.Literal["import"], tuple[str, list[str] | None, int]]
    )

    _Any = _t.Any
    _ModuleT = _t.TypeVar("_ModuleT", bound=types.ModuleType)
    _typing_cast = _t.cast

    @_t.runtime_checkable
    class _DisModule(_t.Protocol):
        def _find_store_names(self, co: types.CodeType) -> _cabc.Generator[str]: ...
        def _find_imports(self, co: types.CodeType) -> _cabc.Generator[tuple[str, int, list[str] | None]]: ...

    assert isinstance(dis, _DisModule)


else:
    _MetaPathFinderProtocol = _OpcodeInfo = _Any = _ModuleT = object

    def _typing_cast(typ, val):
        return val


_NEEDS_LOADING: _Any = object()


def _scan_opcodes(code: types.CodeType) -> _cabc.Generator[_OpcodeInfo]:
    """Scan the code, and yield 'interesting' opcode combinations."""

    for name in dis._find_store_names(code):  # pyright: ignore [reportPrivateUsage]
        yield ("store", (name,))
    for name, level, fromlist in dis._find_imports(code):  # pyright: ignore [reportPrivateUsage]
        yield ("import", (name, fromlist, level))


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


# NOTE: Adapted from importlib._bootstrap._handle_fromlist().
def _handle_fromlist(
    module: _ModuleT,
    fromlist: _cabc.Sequence[object],
    import_: _cabc.Callable[[str], _ModuleT],
    *,
    recursive: bool = False,
    cached_modules: _cabc.Mapping[str, types.ModuleType] | None = None,
) -> _ModuleT:
    """Figure out what __import__ should return.

    The import_ parameter is a callable which takes the name of module to
    import. It is required to decouple the function from assuming importlib's
    import implementation is desired.

    """
    # The hell that is fromlist ...
    # If a package was imported, try to import stuff from fromlist.
    for x in fromlist:
        if not isinstance(x, str):
            if recursive:
                where = module.__name__ + ".__all__"
            else:
                where = "``from list''"
            raise TypeError(f"Item in {where} must be str, not {type(x).__name__}")

        elif x == "*":
            if not recursive and hasattr(module, "__all__"):
                _handle_fromlist(module, module.__all__, import_, recursive=True, cached_modules=cached_modules)

        elif not hasattr(module, x):
            from_name = f"{module.__name__}.{x}"
            try:
                import_(from_name)
            except ModuleNotFoundError as exc:
                # Backwards-compatibility dictates we ignore failed
                # imports triggered by fromlist for modules that don't
                # exist.
                if cached_modules is None:
                    cached_modules = sys.modules
                if exc.name == from_name and cached_modules.get(from_name, _NEEDS_LOADING) is not None:
                    continue
                raise
    return module


class MFModuleType(types.ModuleType):
    """Our stand-in for `types.ModuleType`.

    Attributes
    ----------
    __spec__: importlib.machinery.ModuleSpec
        The module spec, which is always set.
    __code__: types.CodeType | None
        The unexecuted module code.
    global_names: set[str]
        The set of global names that are assigned to within the module. This includes those names imported through
        star-imports of Python modules.
    star_imports: set[str]
        The set of star-imports this module did that could not be resolved, ie. a star-import from a non-Python module.
    """

    __code__: types.CodeType | None
    global_names: set[str]
    star_imports: set[str]


def _convert_to_mf_module(module: types.ModuleType) -> MFModuleType:
    module.__class__ = MFModuleType
    module = _typing_cast(MFModuleType, module)

    # Initialize mf-specific module attributes.
    module.__code__ = None
    module.global_names = set()
    module.star_imports = set()

    return module


class ModuleFinder:
    """A class that tracks module imports recursively.

    Attributes
    ----------
    path: list[str]
        A stand-in for `sys.path`. Defaults to `sys.path`.
    path_replacements: list[tuple[str, str]]
        A list of (oldpath, newpath) tuples that will be replaced in module paths.
    modules: dict[str, MFModuleType]
        A stand-in for `sys.modules`.
    meta_path: list[_MetaPathFinderProtocol]
        A stand-in for `sys.meta_path`.
    """

    def __init__(
        self,
        path: list[str] | None = None,
        path_replacements: list[tuple[str, str]] | None = None,
        excludes: list[str] | None = None,
    ) -> None:
        self.path: list[str] = path if (path is not None) else sys.path
        self.path_replacements: list[tuple[str, str]] = path_replacements if (path_replacements is not None) else []
        self.excludes: list[str] = excludes if (excludes is not None) else []
        self.modules: dict[str, MFModuleType] = {}
        self.meta_path: list[_MetaPathFinderProtocol] = [importlib.machinery.PathFinder]

        self.bad_modules: dict[str, set[str]] = {}  # TODO

    def _scan_code(self, code: types.CodeType, module: MFModuleType) -> None:  # noqa: PLR0912
        for opcode_info in _scan_opcodes(code):
            match opcode_info:
                case ("store", (name,)):
                    module.global_names.add(name)

                case ("import", (name, fromlist, level)):
                    if fromlist is not None:
                        have_star = "*" in fromlist
                        fromlist = [f for f in fromlist if f != "*"]
                    else:
                        have_star = False

                    package = _typing_cast("str | None", importlib._bootstrap._calc___package__(module.__dict__))  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
                    absolute_name = importlib.util.resolve_name(("." * level) + name, package)
                    try:
                        self._import_module(absolute_name, None, fromlist)
                    except (ImportError, SyntaxError):
                        self.bad_modules.setdefault(absolute_name, set()).add(module.__name__)
                        raise

                    if have_star:
                        # We've encountered an "import *". If it is a Python module,
                        # the code has already been parsed and we can suck out the
                        # global names.
                        if (cached_mod := self.modules.get(name)) is not None:
                            module.global_names |= cached_mod.global_names
                            module.star_imports |= cached_mod.star_imports
                            if cached_mod.__code__ is None:
                                module.star_imports.add(name)
                        else:
                            module.star_imports.add(name)

                case unknown:  # pyright: ignore [reportUnnecessaryComparison]
                    # We don't expect anything else from the generator.
                    raise RuntimeError(f"Unknown opcode info: {unknown!r}")

        for const in code.co_consts:
            if isinstance(const, types.CodeType):
                self._scan_code(const, module)

    def _load_and_cache_module(self, name: str, spec: importlib.machinery.ModuleSpec) -> MFModuleType:
        module = importlib.util.module_from_spec(spec)
        module = _convert_to_mf_module(module)
        self.modules[name] = module

        loader = _typing_cast("importlib.abc.InspectLoader", spec.loader)
        code = loader.get_code(module.__name__)
        if code is not None:
            if self.path_replacements:
                code = _replace_paths_in_code(code, self.path_replacements)
            module.__code__ = code
            self._scan_code(code, module)

        return module

    def _import_module(
        self,
        name: str,
        package: str | None = None,
        fromlist: _cabc.Sequence[str] | None = None,
    ) -> MFModuleType:
        """An approximate implementation of import (modified for our purposes).

        Adapted from the importlib import_module() recipe.
        """

        absolute_name = importlib.util.resolve_name(name, package)

        try:
            return self.modules[absolute_name]
        except KeyError:
            pass

        path = self.path  # Override Pathfinder.find_spec's late default of sys.path.
        if "." in absolute_name:
            parent_name, _, child_name = absolute_name.rpartition(".")
            parent_module = self._import_module(parent_name)
            assert parent_module.__spec__ is not None
            path = parent_module.__spec__.submodule_search_locations

        for finder in self.meta_path:
            spec = finder.find_spec(absolute_name, path)
            if spec is not None:
                break
        else:
            msg = f"No module named {absolute_name!r}"
            raise ModuleNotFoundError(msg, name=absolute_name)

        module = self._load_and_cache_module(absolute_name, spec)

        if path is not None:
            setattr(parent_module, child_name, module)  # pyright: ignore [reportPossiblyUnboundVariable]

        if fromlist and hasattr(module, "__path__"):
            return _handle_fromlist(module, fromlist, self._import_module, cached_modules=self.modules)

        return module

    def import_module(self, name: str, package: str | None = None, fromlist: _cabc.Sequence[str] | None = None) -> None:
        self._import_module(name, package, fromlist)

    def import_from_path(self, pathname: str) -> None:
        _dir, tail = os.path.split(pathname)
        name, _ext = os.path.splitext(tail)

        spec = importlib.util.spec_from_file_location(name, pathname)
        assert spec is not None
        self._load_and_cache_module(name, spec)

    def run_script(self, pathname: str) -> None:
        # TODO: This is probably wrong.
        name = "__main__"
        spec = importlib.util.spec_from_file_location(name, pathname)
        assert spec is not None
        self._load_and_cache_module(name, spec)
