# ruff: noqa: T201, PTH120, PTH122, PTH208

"""Find modules used by a script, using introspection."""

from __future__ import annotations

import collections.abc
import dis
import importlib.util
import importlib.machinery
import os
import sys
import types
import typing as t


# Old imp constants:

_PKG_DIRECTORY = 5


# Modulefinder does a good job at simulating Python's, but it can not
# handle __path__ modifications packages make at runtime.  Therefore there
# is a mechanism whereby you can register extra paths in this map for a
# package, and it will be honored.

_package_path_map: dict[str, list[str]] = {}


def add_package_path(package_name: str, path: str) -> None:
    _package_path_map.setdefault(package_name, []).append(path)


_replace_package_map: dict[str, str] = {}


def replace_package(oldname: str, newname: str) -> None:
    """Allow `modulefinder` to work around situations in which a package injects itself under the name of another package
    into `sys.modules` at runtime by calling ``replace_package("real_package_name", "faked_package_name")`` before running
    `ModuleFinder`.
    """

    _replace_package_map[oldname] = newname


def _find_spec_from_path(
    name: str, path: collections.abc.Sequence[str] | None = None
) -> importlib.machinery.ModuleSpec:
    """A wrapper around importlib.machinery.PathFinder.find_spec() (for our own purposes)."""

    # It's necessary to clear the caches for our Finder first, in case any
    # modules are being added/deleted/modified at runtime. In particular,
    # test_modulefinder.py changes file tree contents in a cache-breaking way.
    importlib.machinery.PathFinder.invalidate_caches()

    spec = importlib.machinery.PathFinder.find_spec(name, path)

    if spec is None:
        msg = f"No module named {name!r}"
        raise ImportError(msg, name=name)

    if spec.loader is None:
        msg = "missing loader"
        raise ImportError(msg, name=name)

    return spec


class Module:
    def __init__(self, name: str, file: str | None = None, path: list[str] | None = None) -> None:
        self.__name__: str = name
        self.__file__: str | None = file
        self.__path__: list[str] | None = path
        self.__code__: types.CodeType | None = None
        # The set of global names that are assigned to in the module.
        # This includes those names imported through starimports of
        # Python modules.
        self.globalnames: set[str] = set()
        # The set of starimports this module did that could not be
        # resolved, ie. a starimport from a non-Python module.
        self.starimports: set[str] = set()

    def __repr__(self, /) -> str:
        s = [self.__class__.__name__, "(", f"{self.__name__!r}"]

        if self.__file__ is not None:
            s.append(f", {self.__file__!r}")
        if self.__path__ is not None:
            s.append(f", {self.__path__!r}")
        s.append(")")

        return "".join(s)


class ModuleFinder:
    def __init__(
        self,
        path: list[str] | None = None,
        debug: int = 0,
        excludes: list[str] | None = None,
        replace_paths: list[tuple[str, str]] | None = None,
    ) -> None:
        self.path: list[str] = path if (path is not None) else sys.path
        self.modules: dict[str, Module] = {}
        self.badmodules: dict[str, set[str]] = {}
        self.debug: int = debug
        self.indent: int = 0
        self.excludes: list[str] = excludes if (excludes is not None) else []
        self.replace_paths: list[tuple[str, str]] = replace_paths if (replace_paths is not None) else []
        self.processed_paths: list[str] = []  # Used in debugging only

    def msg(self, level: int, message: str, *args: object) -> None:
        if level <= self.debug:
            print(" " * 4 * self.indent, end="")
            print(message, end=" ")
            print(" ".join(map(repr, args)))

    def msgin(self, level: int, message: str, *args: object) -> None:
        if level <= self.debug:
            self.indent = self.indent + 1
            self.msg(level, message, *args)

    def msgout(self, level: int, message: str, *args: object) -> None:
        if level <= self.debug:
            self.indent = self.indent - 1
            self.msg(level, message, *args)

    def run_script(self, pathname: str) -> None:
        self.msg(2, "run_script", pathname)

        spec = importlib.util.spec_from_file_location("__main__", pathname)
        assert spec is not None
        self.load_module("__main__", spec)

    def load_file(self, pathname: str) -> None:
        _dir, name = os.path.split(pathname)
        name, _ext = os.path.splitext(name)

        spec = importlib.util.spec_from_file_location(name, pathname)
        assert spec is not None
        self.load_module(name, spec)

    def import_hook(
        self,
        name: str,
        caller: Module | None = None,
        fromlist: list[str] | None = None,
        level: int = -1,
    ) -> Module | None:
        self.msg(3, "import_hook", name, caller, fromlist, level)

        parent = self.determine_parent(caller, level=level)
        q, tail = self.find_head_package(parent, name)
        m = self.load_tail(q, tail)
        if not fromlist:
            return q
        if m.__path__:
            self.ensure_fromlist(m, fromlist)
        return None

    def determine_parent(self, caller: Module | None, level: int = -1) -> Module | None:
        self.msgin(4, "determine_parent", caller, level)

        if not caller or level == 0:
            self.msgout(4, "determine_parent -> None")
            return None

        pname = caller.__name__
        if level >= 1:  # relative import
            if caller.__path__:
                level -= 1
            if level == 0:
                parent = self.modules[pname]
                assert parent is caller
                self.msgout(4, "determine_parent ->", parent)
                return parent

            if pname.count(".") < level:
                msg = "relative importpath too deep"
                raise ImportError(msg)

            pname = ".".join(pname.split(".")[:-level])
            parent = self.modules[pname]
            self.msgout(4, "determine_parent ->", parent)
            return parent

        if caller.__path__:
            parent = self.modules[pname]
            assert caller is parent
            self.msgout(4, "determine_parent ->", parent)
            return parent

        if "." in pname:
            i = pname.rfind(".")
            pname = pname[:i]
            parent = self.modules[pname]
            assert parent.__name__ == pname
            self.msgout(4, "determine_parent ->", parent)
            return parent

        self.msgout(4, "determine_parent -> None")
        return None

    def find_head_package(self, parent: Module | None, name: str) -> tuple[Module, str]:
        self.msgin(4, "find_head_package", parent, name)

        head, _, tail = name.partition(".")

        if parent:
            qname = f"{parent.__name__}.{head}"
        else:
            qname = head

        if q := self.import_module(head, qname, parent):
            self.msgout(4, "find_head_package ->", (q, tail))
            return q, tail

        if parent:
            qname = head
            parent = None
            if q := self.import_module(head, qname, parent):
                self.msgout(4, "find_head_package ->", (q, tail))
                return q, tail

        self.msgout(4, "raise ImportError: No module named", qname)
        raise ImportError("No module named " + qname)

    def load_tail(self, q: Module, tail: str) -> Module:
        self.msgin(4, "load_tail", q, tail)
        m = q
        while tail:
            head, _, tail = tail.partition(".")
            mname = f"{m.__name__}.{head}"
            m = self.import_module(head, mname, m)
            if not m:
                self.msgout(4, "raise ImportError: No module named", mname)
                raise ImportError("No module named " + mname)
        self.msgout(4, "load_tail ->", m)
        return m

    def ensure_fromlist(self, m: Module, fromlist: list[str], recursive: bool = False) -> None:
        self.msg(4, "ensure_fromlist", m, fromlist, recursive)
        for sub in fromlist:
            if sub == "*":
                if not recursive and (all_ := self.find_all_submodules(m)):
                    self.ensure_fromlist(m, all_, recursive=True)
            elif not hasattr(m, sub):
                subname = f"{m.__name__}.{sub}"
                submod = self.import_module(sub, subname, m)
                if not submod:
                    raise ImportError("No module named " + subname)

    def find_all_submodules(self, m: Module) -> list[str] | None:
        if not m.__path__:
            return None

        modules: dict[str, str] = {}
        # 'suffixes' used to be a list hardcoded to [".py", ".pyc"].
        # But we must also collect Python extension modules - although
        # we cannot separate normal dlls from Python extensions.
        suffixes = (
            importlib.machinery.EXTENSION_SUFFIXES
            + importlib.machinery.SOURCE_SUFFIXES
            + importlib.machinery.BYTECODE_SUFFIXES
        )
        for dir_ in m.__path__:
            try:
                names = os.listdir(dir_)
            except OSError:
                self.msg(2, "can't list directory", dir_)
                continue

            for name in names:
                for suff in suffixes:
                    if name.endswith(suff):
                        mod = name.removesuffix(suff)
                        break
                else:
                    mod = None

                if mod and mod != "__init__":
                    modules[mod] = mod

        return list(modules.keys())

    def import_module(self, partname: str, fqname: str, parent: Module | None) -> Module | None:
        self.msgin(3, "import_module", partname, fqname, parent)
        try:
            m = self.modules[fqname]
        except KeyError:
            pass
        else:
            self.msgout(3, "import_module ->", m)
            return m
        if fqname in self.badmodules:
            self.msgout(3, "import_module -> None")
            return None
        if parent and parent.__path__ is None:
            self.msgout(3, "import_module -> None")
            return None
        try:
            spec = self.find_module(partname, parent and parent.__path__, parent)
        except ImportError:
            self.msgout(3, "import_module ->", None)
            return None

        m = self.load_module(fqname, spec)
        if parent:
            setattr(parent, partname, m)
        self.msgout(3, "import_module ->", m)
        return m

    def load_module(self, fqname: str, spec: importlib.machinery.ModuleSpec) -> Module:
        self.msgin(2, "load_module", fqname, spec)
        if spec.submodule_search_locations is not None:
            m = self.load_package(fqname, spec)
            self.msgout(2, "load_module ->", m)
            return m

        # if type_ == _PY_SOURCE:
        #     co = compile(fp.read(), pathname, "exec")
        # elif type_ == _PY_COMPILED:
        #     try:
        #         data = fp.read()
        #         importlib._bootstrap_external._classify_pyc(data, fqname, {})  # pyright: ignore [reportAttributeAccessIssue, reportUnknownMemberType]
        #     except ImportError as exc:
        #         self.msgout(2, "raise ImportError: " + str(exc), pathname)
        #         raise
        #     co = marshal.loads(memoryview(data)[16:])  # noqa: S302
        # else:
        #     co = None

        _real_mod = importlib.util.module_from_spec(spec)

        assert spec.loader is not None
        assert hasattr(spec.loader, "get_code")
        co = spec.loader.get_code(fqname)
        assert isinstance(co, types.CodeType)

        m = self.add_module(fqname)
        m.__file__ = _real_mod.__file__
        if co:
            if self.replace_paths:
                co = self.replace_paths_in_code(co)
            m.__code__ = co
            self.scan_code(co, m)
        self.msgout(2, "load_module ->", m)
        return m

    def _add_badmodule(self, name: str, caller: Module | None) -> None:
        self.badmodules.setdefault(name, set()).add(caller.__name__ if caller else "-")

    def _safe_import_hook(self, name: str, caller: Module | None, fromlist: list[str] | None, level: int = -1) -> None:
        # wrapper for self.import_hook() that won't raise ImportError
        if name in self.badmodules:
            self._add_badmodule(name, caller)
            return
        try:
            self.import_hook(name, caller, level=level)
        except ImportError as msg:
            self.msg(2, "ImportError:", str(msg))
            self._add_badmodule(name, caller)
        except SyntaxError as msg:
            self.msg(2, "SyntaxError:", str(msg))
            self._add_badmodule(name, caller)
        else:
            if fromlist:
                for sub in fromlist:
                    fullname = name + "." + sub
                    if fullname in self.badmodules:
                        self._add_badmodule(fullname, caller)
                        continue
                    try:
                        self.import_hook(name, caller, [sub], level=level)
                    except ImportError as msg:
                        self.msg(2, "ImportError:", str(msg))
                        self._add_badmodule(fullname, caller)

    def scan_opcodes(
        self,
        co: types.CodeType,
    ) -> collections.abc.Generator[
        tuple[t.Literal["store"], tuple[str]]
        | tuple[t.Literal["absolute_import"], tuple[list[str] | None, str]]
        | tuple[t.Literal["relative_import"], tuple[int, list[str] | None, str]]
    ]:
        # Scan the code, and yield 'interesting' opcode combinations
        for name in dis._find_store_names(co):  # pyright: ignore [reportAttributeAccessIssue, reportUnknownMemberType]
            yield "store", (name,)
        for name, level, fromlist in dis._find_imports(co):  # pyright: ignore [reportAttributeAccessIssue, reportUnknownMemberType]
            if level == 0:  # absolute import
                yield "absolute_import", (fromlist, name)
            else:  # relative import
                yield "relative_import", (level, fromlist, name)

    def scan_code(self, co: types.CodeType, m: Module) -> None:  # noqa: PLR0912
        scanner = self.scan_opcodes
        for args in scanner(co):
            match args:
                case ("store", (name,)):
                    m.globalnames.add(name)
                case ("absolute_import", (fromlist, name)):
                    have_star = 0
                    if fromlist is not None:
                        if "*" in fromlist:
                            have_star = 1
                        fromlist = [f for f in fromlist if f != "*"]
                    self._safe_import_hook(name, m, fromlist, level=0)
                    if have_star:
                        # We've encountered an "import *". If it is a Python module,
                        # the code has already been parsed and we can suck out the
                        # global names.
                        mm = None
                        if m.__path__:
                            # At this point we don't know whether 'name' is a
                            # submodule of 'm' or a global module. Let's just try
                            # the full name first.
                            mm = self.modules.get(m.__name__ + "." + name)

                        if mm is None:
                            mm = self.modules.get(name)

                        if mm is None:
                            m.starimports.add(name)
                        else:
                            m.globalnames |= mm.globalnames
                            m.starimports |= mm.starimports
                            if mm.__code__ is None:
                                m.starimports.add(name)

                case ("relative_import", (level, fromlist, name)):
                    if name:
                        self._safe_import_hook(name, m, fromlist, level=level)
                    else:
                        parent = self.determine_parent(m, level=level)
                        self._safe_import_hook(parent.__name__, None, fromlist, level=0)
                case (what, _):  # pyright: ignore [reportUnnecessaryComparison]
                    # We don't expect anything else from the generator.
                    raise RuntimeError(what)

        for c in co.co_consts:
            if isinstance(c, types.CodeType):
                self.scan_code(c, m)

    def load_package(self, fqname: str, spec: importlib.machinery.ModuleSpec) -> Module:
        self.msgin(2, "load_package", fqname, spec)
        newname = _replace_package_map.get(fqname)
        if newname:
            fqname = newname
        m = self.add_module(fqname)
        m.__file__ = spec.origin
        m.__path__ = spec.submodule_search_locations

        # As per comment at top of file, simulate runtime __path__ additions.
        if m.__path__ is not None:
            for runtime_addition in _package_path_map.get(fqname, []):
                m.__path__.append(runtime_addition)

        spec = self.find_module("__init__", m.__path__)
        self.load_module(fqname, spec)
        self.msgout(2, "load_package ->", m)
        return m

    def add_module(self, fqname: str) -> Module:
        m = self.modules.get(fqname)
        if not m:
            self.modules[fqname] = m = Module(fqname)
        return m

    def find_module(
        self,
        name: str,
        path: collections.abc.Sequence[str] | None,
        parent: Module | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        if parent is not None:
            # assert path is not None
            fullname = parent.__name__ + "." + name
        else:
            fullname = name

        if fullname in self.excludes:
            self.msgout(3, "find_module -> Excluded", fullname)
            raise ImportError(name)

        if path is None:
            if name in sys.builtin_module_names:
                return None

            path = self.path

        return _find_spec_from_path(name, path)

    def report(self) -> None:
        """Print a report to stdout, listing the found modules with their
        paths, as well as modules that are missing, or seem to be missing.
        """
        print()
        print(f"  {'Name':25} File")
        print(f"  {'----':25} ----")
        # Print modules found
        keys = sorted(self.modules.keys())
        for key in keys:
            m = self.modules[key]
            if m.__path__:
                print("P", end=" ")
            else:
                print("m", end=" ")
            print(f"{key:25} {m.__file__ or ''}")

        # Print missing modules
        missing, maybe = self.any_missing_maybe()
        if missing:
            print()
            print("Missing modules:")
            for name in missing:
                mods = sorted(self.badmodules[name])
                print("?", name, "imported from", ", ".join(mods))
        # Print modules that may be missing, but then again, maybe not...
        if maybe:
            print()
            print("Submodules that appear to be missing, but could also be", end=" ")
            print("global names in the parent package:")
            for name in maybe:
                mods = sorted(self.badmodules[name])
                print("?", name, "imported from", ", ".join(mods))

    def any_missing(self) -> list[str]:
        """Return a list of modules that appear to be missing. Use
        any_missing_maybe() if you want to know which modules are
        certain to be missing, and which *may* be missing.
        """
        missing, maybe = self.any_missing_maybe()
        return missing + maybe

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
        for name in self.badmodules:
            if name in self.excludes:
                continue
            i = name.rfind(".")
            if i < 0:
                missing.append(name)
                continue
            subname = name[i + 1 :]
            pkgname = name[:i]
            pkg = self.modules.get(pkgname)
            if pkg is not None:
                if pkgname in self.badmodules[name]:
                    # The package tried to import this module itself and
                    # failed. It's definitely missing.
                    missing.append(name)
                elif subname in pkg.globalnames:
                    # It's a global in the package: definitely not missing.
                    pass
                elif pkg.starimports:
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

    def replace_paths_in_code(self, co: types.CodeType) -> types.CodeType:
        new_filename = original_filename = os.path.normpath(co.co_filename)
        for f, r in self.replace_paths:
            if original_filename.startswith(f):
                new_filename = r + original_filename.removeprefix(f)
                break

        if self.debug and (original_filename not in self.processed_paths):
            if new_filename != original_filename:
                self.msgout(2, f"co_filename {original_filename!r} changed to {new_filename!r}")
            else:
                self.msgout(2, f"co_filename {original_filename!r} remains unchanged")
            self.processed_paths.append(original_filename)

        consts = list(co.co_consts)
        for i, const in enumerate(consts):
            if isinstance(const, types.CodeType):
                consts[i] = self.replace_paths_in_code(const)

        return co.replace(co_consts=tuple(consts), co_filename=new_filename)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("-d", "--debug", action="count")
    parser.add_argument("-m", "--module", action="append", dest="modules_to_do")
    parser.add_argument("-p", "--addpath", action="append")
    parser.add_argument("-x", "--exclude", action="append")
    parser.add_argument("scripts", nargs="*", default=["hello.py"])

    class Namespace(argparse.Namespace):
        debug: int
        modules_to_do: list[str]
        addpath: list[str]
        exclude: list[str]
        scripts: list[str]

    args = parser.parse_args(namespace=Namespace())

    # Set the path based on sys.path and the script directory
    script = args.scripts[0]
    path = (
        [subpath for path in args.addpath for subpath in path.split(os.pathsep)]
        + [os.path.dirname(script)]
        + sys.path[1:]
    )
    if args.debug:
        print("path:")
        for item in path:
            print(f"    {item}")

    # Create the module finder and turn its crank
    mf = ModuleFinder(path, args.debug, args.exclude)

    for mod in args.modules_to_do:
        if mod.endswith(".*"):
            mf.import_hook(mod.removesuffix(".*"), None, ["*"])
        else:
            mf.import_hook(mod)

    for file in args.scripts:
        mf.load_file(file)

    mf.run_script(script)
    mf.report()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
