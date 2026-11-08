"""The raw adapter removes one explicit constraint and no other renderer logic."""
import ast
from pathlib import Path
import sys
import pytest


def adapter():
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
    import langsplat_raw3
    return langsplat_raw3


def test_raw_renderer_changes_exactly_the_feature_normalization():
    module = adapter()
    path = module.ref.UP/'gaussian_renderer/__init__.py'
    if not path.exists():
        pytest.skip('Pinned renderer unavailable')
    source = path.read_text()
    expected = ast.parse(source)
    removed = 0
    for node in ast.walk(expected):
        if isinstance(node,ast.If) and ast.unparse(node.test)=='opt.include_feature':
            assert ast.unparse(node.body[1])=='language_feature_precomp = language_feature_precomp / (language_feature_precomp.norm(dim=-1, keepdim=True) + 1e-09)'
            node.body.pop(1)
            removed += 1
    assert removed==1
    actual = module.raw_renderer_tree(source)
    assert ast.dump(actual)==ast.dump(expected)
    compile(actual,'raw-renderer-test','exec')


def test_adapter_refuses_changed_or_ambiguous_normalization():
    module = adapter()
    for source in ('x=1','language_feature_precomp=a/b\nlanguage_feature_precomp=c/d'):
        with pytest.raises(ValueError,match='exactly one'):
            module.raw_renderer_tree(source)
