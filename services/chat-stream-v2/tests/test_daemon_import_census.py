"""Synthetic controls for the non-executing import census."""
import importlib.util
import sys
from pathlib import Path

path = Path(__file__).resolve().parents[1] / 'tools' / 'daemon_import_census.py'
spec = importlib.util.spec_from_file_location('daemon_import_census', path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_census_static_dynamic_nested_and_unresolved():
    source = '''import importlib as il
from importlib import import_module as load
from .feature import thing as t
alias = load
class C:
    def run(self, name):
        import hidden as h
        il.import_module("foo")
        alias("bar")
        __import__(name)
'''
    sites = module.census(source, 'pkg/core.py')
    assert sites == module.census(source, 'pkg/core.py')
    assert any(s.form == 'from' and s.module == 'pkg.feature' and s.alias == 't' for s in sites)
    assert any(s.module == 'hidden' and s.scope == 'C.run' and s.alias == 'h' for s in sites)
    assert {s.module for s in sites if s.form == 'dynamic'} == {'foo', 'bar'}
    assert any(s.form == 'unresolved' and s.scope == 'C.run' for s in sites)


def test_relative_dynamic_and_unresolved_wrapper():
    source = '''from importlib import import_module as load

def wrapper(name):
    return load(name)

def run():
    return load(".feature", "pkg"), load(".unknown", package=dynamic)
'''
    sites = module.census(source, 'pkg/core.py')
    assert any(s.module == 'pkg.feature' and s.form == 'dynamic' for s in sites)
    assert sum(s.form == 'unresolved' for s in sites) == 2


def test_module_alias_chain_and_rebinding_are_conservative():
    source = '''import importlib as il
loader = il
get = loader.import_module

def first():
    return get("feature")

def second():
    get = harmless
    return get(computed)
'''
    sites = module.census(source, 'core.py')
    assert any(s.form == 'dynamic' and s.module == 'feature' for s in sites)
    assert any(s.form == 'unresolved' and s.scope == 'second' for s in sites)


def test_getattr_importer_alias_is_detected():
    source = '''import importlib as imports
get = getattr(imports, "import_module")
get("projection")
get(dynamic_name)
'''
    sites = module.census(source, 'core.py')
    assert any(s.module == 'projection' and s.form == 'dynamic' for s in sites)
    assert any(s.form == 'unresolved' for s in sites)


def test_repeated_import_on_one_line_has_stable_order():
    sites = module.census('from feature import item, item as alias\n', 'core.py')
    assert [s.alias for s in sites] == [None, 'alias']


def test_computed_importer_attribute_is_unresolved():
    sites = module.census('import importlib as lib\ngetattr(lib, choice)(name)\n', 'core.py')
    assert any(site.form == 'unresolved' for site in sites)


def test_builtin_import_level_resolution_and_unresolved_cases():
    absolute = module.census("__import__('child', level=0)\n", 'pkg/core.py')
    relative = module.census("__import__('child', globals(), locals(), (), 1)\n", 'pkg/core.py')
    computed = module.census("__import__('child', level=unknown)\n", 'pkg/core.py')
    beyond = module.census("__import__('child', level=2)\n", 'pkg/core.py')
    top_level = module.census("__import__('child', level=1)\n", 'core.py')
    nested = module.census("__import__('child', globals(), level=2)\n", 'pkg/sub/core.py')
    unpacked = module.census("__import__('child', **options)\n", 'pkg/core.py')
    unknown_globals = module.census("__import__('child', foreign_globals, level=1)\n", 'pkg/core.py')
    omitted_globals = module.census("__import__('child', level=1)\n", 'pkg/core.py')
    explicit_mapping = module.census("__import__('child', {'__package__': 'pkg'}, level=1)\n", 'pkg/core.py')
    shadowed_globals = module.census("globals = fake\n__import__('child', globals(), level=1)\n", 'pkg/core.py')
    assert absolute[0].form == 'dynamic' and absolute[0].module == 'child'
    assert relative[0].form == 'dynamic' and relative[0].module == 'pkg.child'
    assert nested[0].form == 'dynamic' and nested[0].module == 'pkg.child'
    assert all(sites[0].form == 'unresolved' for sites in (computed, beyond, top_level, unpacked, unknown_globals, omitted_globals, explicit_mapping, shadowed_globals))


def test_unrelated_globals_parameter_does_not_hide_current_scope_builtin():
    source = '''def unrelated(globals):
    return globals

def load():
    return __import__('child', globals(), level=1)
'''
    sites = module.census(source, 'pkg/core.py')
    assert len(sites) == 1 and sites[0].form == 'dynamic' and sites[0].module == 'pkg.child'


def test_same_scope_globals_parameter_is_unresolved():
    source = '''def load(globals):
    return __import__('child', globals(), level=1)
'''
    sites = module.census(source, 'pkg/core.py')
    assert len(sites) == 1 and sites[0].form == 'unresolved'


def test_class_globals_binding_does_not_shadow_method_builtin():
    source = '''class Container:
    globals = fake
    def load(self):
        return __import__('child', globals(), level=1)
'''
    sites = module.census(source, 'pkg/core.py')
    assert len(sites) == 1 and sites[0].form == 'dynamic' and sites[0].module == 'pkg.child'


def test_default_expression_uses_enclosing_globals_scope():
    source = '''def load(globals=__import__('child', globals(), level=1)):
    return globals
'''
    sites = module.census(source, 'pkg/core.py')
    assert len(sites) == 1 and sites[0].form == 'dynamic' and sites[0].module == 'pkg.child'
