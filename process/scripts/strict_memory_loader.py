"""Strict YAML loader for memory frontmatter.

Subclasses yaml.SafeLoader with three additional safety constraints:

1. Anchors (`&name`) and aliases (`*name`) are rejected — memory frontmatter
   should be literal data, not graph-structured YAML.
2. Duplicate mapping keys are rejected — silent last-value-wins is a footgun.
3. Explicit YAML tags (`!str`, `!!python/object`, etc.) are rejected on
   scalar values — memory frontmatter uses only the core JSON-compatible
   YAML subset.

The bundled memory_frontmatter.py helper uses this loader so catalog
generation and validation apply the same parsing constraints.
"""

import yaml


class StrictLoaderError(yaml.YAMLError):
    """Raised when memory frontmatter violates the strict loader policy."""


class StrictMemoryLoader(yaml.SafeLoader):
    pass


def _reject_anchor(loader, node):
    raise StrictLoaderError(
        f"YAML anchors are not allowed in memory frontmatter (at {node.start_mark})"
    )


def _reject_alias(loader, node):
    raise StrictLoaderError(
        f"YAML aliases are not allowed in memory frontmatter (at {node.start_mark})"
    )


def _construct_mapping_strict(loader, node, deep=False):
    if not isinstance(node, yaml.MappingNode):
        raise StrictLoaderError(
            f"expected a mapping node, got {type(node).__name__} at {node.start_mark}"
        )
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise StrictLoaderError(
                f"duplicate key {key!r} in mapping at {key_node.start_mark}"
            )
        value = loader.construct_object(value_node, deep=deep)
        mapping[key] = value
    return mapping


# Register strict overrides
StrictMemoryLoader.add_constructor(
    "tag:yaml.org,2002:map",
    _construct_mapping_strict,
)


# Reject anchor/alias by patching compose_node to detect & and *.
_original_compose_node = StrictMemoryLoader.compose_node


def _strict_compose_node(self, parent, index):
    # Track aliases by intercepting the alias path.
    if self.check_event(yaml.AliasEvent):
        event = self.get_event()
        raise StrictLoaderError(
            f"YAML aliases are not allowed in memory frontmatter "
            f"(alias *{event.anchor} at {event.start_mark})"
        )
    if self.check_event(yaml.NodeEvent):
        event = self.peek_event()
        if event.anchor is not None:
            raise StrictLoaderError(
                f"YAML anchors are not allowed in memory frontmatter "
                f"(anchor &{event.anchor} at {event.start_mark})"
            )
    return _original_compose_node(self, parent, index)


StrictMemoryLoader.compose_node = _strict_compose_node


# Reject explicit YAML tags on scalars. Memory frontmatter must use the
# core implicit-resolution subset; explicit tags like !!str or !!int can
# bypass the intended numeric/string distinction (e.g. `a: !!str 0123`
# turns an octal-looking literal into a string, masking the policy).
#
# We override compose_scalar_node to refuse any scalar with a non-default
# explicit tag. PyYAML's resolver uses tag "!" or "" for unresolved scalars
# and core tags (tag:yaml.org,2002:str, :int, :float, :bool, :null,
# :timestamp) for implicit resolution — we accept both. Anything else
# (explicit !!str, !!int, !str, custom tags) is rejected.
_IMPLICIT_CORE_TAGS = frozenset({
    "tag:yaml.org,2002:str",
    "tag:yaml.org,2002:int",
    "tag:yaml.org,2002:float",
    "tag:yaml.org,2002:bool",
    "tag:yaml.org,2002:null",
    "tag:yaml.org,2002:timestamp",
})


_original_construct_scalar = yaml.SafeLoader.construct_scalar


def _construct_scalar_strict(loader, node):
    # Detect explicit tag application: PyYAML records the original tag style
    # in node.tag. For implicit-resolution scalars, the resolver assigns one
    # of the core tags AFTER scanning. The way to detect explicit tags is to
    # check whether the source had a tag token — exposed via node.style is
    # not reliable, so we rely on the loader's tag-resolution path: if the
    # node's tag is a core tag, it could be implicit OR explicit. Since
    # PyYAML doesn't preserve "was this tag explicit" by default, we use a
    # custom approach: register a constructor for each core tag that
    # the implicit resolver also reaches, but checks the original event.
    return _original_construct_scalar(loader, node)


def _process_scalar_strict(self):
    # Hook into the scanner: when an explicit tag token is consumed and the
    # next node is a scalar, raise. The cleanest hook point is to override
    # compose_scalar_node, which receives the constructed ScalarNode and can
    # see whether the tag came from an explicit handle.
    return _original_compose_node(self, None, None)


# The reliable hook: override the parser-level tag detection by checking
# event.tag at compose time. PyYAML's ScalarEvent has a `tag` attribute
# that is None for implicitly-tagged scalars and a string for explicitly-
# tagged ones. We check that BEFORE construction.
_original_compose_scalar_node = StrictMemoryLoader.compose_scalar_node


def _strict_compose_scalar_node(self, anchor):
    event = self.peek_event()
    if isinstance(event, yaml.ScalarEvent) and event.tag is not None:
        # Explicit tag in source. Reject all explicit tags on memory scalars.
        raise StrictLoaderError(
            f"explicit YAML tag {event.tag!r} is not allowed on scalars in memory frontmatter "
            f"(at {event.start_mark})"
        )
    return _original_compose_scalar_node(self, anchor)


StrictMemoryLoader.compose_scalar_node = _strict_compose_scalar_node


def load_frontmatter_strict(text: str):
    """Parse a YAML frontmatter string with strict rules.

    Returns the parsed Python object (dict for memory frontmatter), or raises
    StrictLoaderError on policy violation, or yaml.YAMLError on syntax error.
    """
    return yaml.load(text, Loader=StrictMemoryLoader)
