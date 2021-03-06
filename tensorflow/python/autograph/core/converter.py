# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Converter construction support.

This module contains a base class for all converters, as well as supporting
structures. These structures are referred to as contexts.

The class hierarchy is as follows:

    <your converter>
      [extends] converter.Base
        [extends] transformer.Base
            [extends] gast.nodeTransformer
          [uses] transfomer.SourceInfo
        [uses] converter.EntityContext
          [uses] converter.ProgramContext
          [uses] transfomer.SourceInfo

converter.Base is a specialization of transformer.Base for AutoGraph. It's a
very lightweight subclass that adds a `ctx` attribute holding the corresponding
EntityContext object (see below). Note that converters are not reusable, and
`visit` will raise an error if called more than once.

converter.EntityContext contains mutable state associated with an entity that
the converter processes.

converter.ProgramContext contains mutable state across related entities. For
example, when converting several functions that call one another, the
ProgramContext should be shared across these entities.

Below is the overall flow at conversion:

    program_ctx = ProgramContext(<entities to convert>, <global settings>, ...)
    while <program_ctx has more entities to convert>:
      entity, source_info = <get next entity from program_ctx>
      entity_ctx = EntityContext(program_ctx, source_info)
      for <each ConverterClass>:
        converter = ConverterClass(entity_ctx)

        # May update entity_ctx and program_ctx
        entity = converter.visit(entity)

      <add entity's dependencies to program_ctx>

Note that pyct contains a small number of transformers used for static analysis.
These implement transformer.Base, rather than converter.Base, to avoid a
dependency on AutoGraph.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from enum import Enum

from tensorflow.python.autograph.core import config
from tensorflow.python.autograph.core import naming
from tensorflow.python.autograph.pyct import anno
from tensorflow.python.autograph.pyct import ast_util
from tensorflow.python.autograph.pyct import cfg
from tensorflow.python.autograph.pyct import compiler
from tensorflow.python.autograph.pyct import inspect_utils
from tensorflow.python.autograph.pyct import parser
from tensorflow.python.autograph.pyct import qual_names
from tensorflow.python.autograph.pyct import templates
from tensorflow.python.autograph.pyct import transformer
from tensorflow.python.autograph.pyct.static_analysis import activity
from tensorflow.python.autograph.pyct.static_analysis import live_values
from tensorflow.python.autograph.pyct.static_analysis import liveness
from tensorflow.python.autograph.pyct.static_analysis import reaching_definitions
from tensorflow.python.autograph.pyct.static_analysis import type_info

# TODO(mdan): These contexts can be refactored into first class objects.
# For example, we could define Program and Entity abstractions that hold on
# to the actual entity and have conversion methods.

# TODO(mdan): Add a test specific to this converter.


class Feature(Enum):
  """Constants to use when selecting AutoGraph features."""

  ALL = 'Enable all features.'

  AUTO_CONTROL_DEPS = (
      'Insert of control dependencies in the generated code.')
  DECORATORS = (
      'Allow decorators in local functions. Note that special decorators, '
      ' like ag.convert or tf.function are allowed regardless of this toggle.')
  LISTS = 'Convert list idioms, like initializers, slices, append, etc.'

  def __repr__(self):
    return self.name


class ConversionOptions(object):
  """Immutable container for global conversion flags.

  Attributes:
    recursive: bool, whether to recursively convert any user functions or
      classes that the converted function may use.
    verbose: bool, whether to log the converted code.
    strip_decorators: Tuple[Callable], contains decorators that should be in
      excluded from the compiled output. By default, when converting a function
      before the decorators are applied, the compiled output will include those
      decorators.
    force_conversion: bool, whether to force convertinng the target entity. When
      force_conversion is turned off, the converter may decide to return the
      function as-is.
    optional_features: Union[Feature, Set[Feature]], controls the use of
      optional features in the conversion process. See Feature for available
      options.
  """

  def __init__(self,
               recursive=False,
               verbose=False,
               strip_decorators=None,
               force_conversion=False,
               optional_features=Feature.ALL):
    self.recursive = recursive
    self.verbose = verbose
    self.strip_decorators = strip_decorators or ()
    self.force_conversion = force_conversion

    if isinstance(optional_features, Feature):
      optional_features = (optional_features,)
    optional_features = frozenset(optional_features)
    self.optional_features = optional_features

  def uses(self, feature):
    return (Feature.ALL in self.optional_features or
            feature in self.optional_features)

  def to_ast(self, namespace):
    """Returns a representation of this object as an AST node.

    The AST node encodes a constructor that would create an object with the
    same contents.

    Args:
      namespace: Dict[str, Any], the namespace to use when serializing values to
        names.

    Returns:
      ast.Node
    """
    template = """
      constructor_name(
          recursive=recursive_val,
          verbose=verbose_val,
          strip_decorators=strip_decorators_val,
          force_conversion=force_conversion_val,
          optional_features=optional_features_val)
    """

    def as_qualified_name(o):
      name = inspect_utils.getqualifiedname(namespace, o)
      if not name:
        raise ValueError('Could not locate entity {} in {}'.format(
            o, namespace))
      return name

    def list_of_names(values):
      return parser.parse_expression('({})'.format(', '.join(
          tuple(as_qualified_name(v) for v in values))))

    def list_of_features(values):
      return parser.parse_expression('({})'.format(', '.join(
          'ag__.Feature.{}'.format(v)
          for v in Feature.__members__
          if v in values)))

    expr_ast = templates.replace(
        template,
        constructor_name=parser.parse_expression(
            as_qualified_name(ConversionOptions)),
        recursive_val=parser.parse_expression(str(self.recursive)),
        verbose_val=parser.parse_expression(str(self.verbose)),
        strip_decorators_val=list_of_names(self.strip_decorators),
        force_conversion_val=parser.parse_expression(
            str(self.force_conversion)),
        optional_features_val=list_of_features(self.optional_features))
    return expr_ast[0].value


class ProgramContext(object):
  """ProgramContext keeps track of converting function hierarchies.

  This object is mutable, and is updated during conversion. Not thread safe.

  Attributes:
    options: ConversionOptions
    dependency_cache: Dict[Any, ast.AST], the original entities mapped to their
      converted AST
    additional_imports: Set[Any], additional entities which for any reason
      cannot be attached after loading and need to be explicitly imported in the
      generated code
    name_map: Dict[str, str], map of original entity name to the name of their
      converted counterparts
    autograph_module: Module, a reference to the autograph module. This needs to
      be specified by the caller to avoid circular dependencies.
    uncompiled_modules: Set[Tuple[str, ...]], with each tuple representing the
      fully qualified name of a package containing functions that will not be
      compiled.
    required_imports: str, containing an import statement on each line. These
      are all the imports necessary for the compiled code to run, in addition to
      the closures of each entity, which are attached dynamically.
  """

  def __init__(
      self,
      options,
      partial_types,
      autograph_module,
      uncompiled_modules,
  ):
    self.options = options
    self.partial_types = partial_types if partial_types else ()
    self.autograph_module = autograph_module
    self.uncompiled_modules = uncompiled_modules

    self.conversion_order = []
    self.dependency_cache = {}
    self.additional_imports = set()
    self.name_map = {}

  @property
  def required_imports(self):
    """Returns a block containing all imports required by the converted code."""
    # TODO(mdan): Check that these don't clobber one another.
    return '\n'.join(config.COMPILED_IMPORT_STATEMENTS +
                     tuple(self.additional_imports))

  def new_namer(self, namespace):
    return naming.Namer(namespace, self.options.recursive, self.name_map,
                        self.partial_types)

  def update_name_map(self, namer):
    """Updates renamed_calls based on the recent activity from the namer.

    Whenever we convert a new entity, any references to other entities are being
    renamed to match their soon-to-be-converted counterparts. The namer keeps
    track of these renames. When conversion is complete, we copy those renames
    so that when those referenced entities are being converted, their new name
    matches.

    Args:
      namer: naming.Namer

    Raises:
      ValueError: when an entity was renamed twice and to different names.
    """
    # TODO(mdan): Have call_trees do this directly.
    # This is done so indirectly, via the namer, for historic reasons. But
    # now we can have the converter that does the rename record the new name
    # as well and skip this step altogether.
    for o, name in namer.renamed_calls.items():
      if o in self.name_map:
        if self.name_map[o] != name:
          raise ValueError(
              'Calls to %s were converted using multiple names (%s). This is '
              'possible when an entity with one of these names already '
              'existed. To fix, avoid using any of these names.' %
              (o, (name, self.name_map[o])))
      else:
        self.name_map[o] = name

  def add_to_cache(self, original_entity, converted_ast):
    self.conversion_order.append(original_entity)
    self.dependency_cache[original_entity] = converted_ast


class EntityContext(object):
  """Tracks the conversion of a single entity.

  This object is mutable, and is updated during conversion. Not thread safe.

  Attributes:
    namer: Namer
    info: transformer.EntityInfo
    program: ProgramContext
  """

  def __init__(self, namer, entity_info, program_ctx):
    self.namer = namer
    self.info = entity_info
    self.program = program_ctx


class Base(transformer.Base):
  """All converters should inherit from this class.

  Attributes:
    ctx: EntityContext
  """

  def __init__(self, ctx):
    super(Base, self).__init__(ctx.info)
    self.ctx = ctx  # Keeping this short because it's used frequently.

    self._used = False
    self._ast_depth = 0

  def get_definition_directive(self, node, directive, arg, default):
    """Returns the unique directive argument for a symbol.

    See lang/directives.py for details on directives.

    Example:
       # Given a directive in the code:
       ag.foo_directive(bar, baz=1)

       # One can write for an AST node Name(id='bar'):
       get_definition_directive(node, ag.foo_directive, 'baz')

    Args:
      node: ast.AST, the node representing the symbol for which the directive
        argument is needed.
      directive: Callable[..., Any], the directive to search.
      arg: str, the directive argument to return.
      default: Any

    Raises:
      ValueError: if conflicting annotations have been found
    """
    defs = anno.getanno(node, anno.Static.ORIG_DEFINITIONS, ())
    if not defs:
      return default

    arg_values_found = []
    for def_ in defs:
      if (directive in def_.directives and arg in def_.directives[directive]):
        arg_values_found.append(def_.directives[directive][arg])

    if not arg_values_found:
      return default

    if len(arg_values_found) == 1:
      return arg_values_found[0]

    # If multiple annotations reach the symbol, they must all match. If they do,
    # return any of them.
    first_value = arg_values_found[0]
    for other_value in arg_values_found[1:]:
      if not ast_util.matches(first_value, other_value):
        qn = anno.getanno(node, anno.Basic.QN)
        raise ValueError('%s has ambiguous annotations for %s(%s): %s, %s' %
                         (qn, directive.__name__, arg,
                          compiler.ast_to_source(other_value).strip(),
                          compiler.ast_to_source(first_value).strip()))
    return first_value

  def visit(self, node):
    if not self._ast_depth:
      if self._used:
        raise ValueError('converter objects cannot be reused')
      self._used = True

    self._ast_depth += 1
    try:
      return super(Base, self).visit(node)
    finally:
      self._ast_depth -= 1


class AnnotatedDef(reaching_definitions.Definition):

  def __init__(self):
    super(AnnotatedDef, self).__init__()
    self.directives = {}


class AgAnno(Enum):
  """Annotation labels specific to AutoGraph. See anno.py."""

  DIRECTIVES = 'User directives associated with the annotated statement.'

  def __repr__(self):
    return self.name


def standard_analysis(node, context, is_initial=False):
  """Performs a complete static analysis of the given code.

  Args:
    node: ast.AST
    context: converter.EntityContext
    is_initial: bool, whether this is the initial analysis done on the input
      source code

  Returns:
    ast.AST, same as node, with the static analysis annotations added
  """
  # TODO(mdan): Clear static analysis here.
  # TODO(mdan): Consider not running all analyses every time.
  # TODO(mdan): Don't return a node because it's modified by reference.
  graphs = cfg.build(node)
  node = qual_names.resolve(node)
  node = activity.resolve(node, context.info, None)
  node = reaching_definitions.resolve(node, context.info, graphs, AnnotatedDef)
  node = liveness.resolve(node, context.info, graphs)
  node = live_values.resolve(node, context.info, config.PYTHON_LITERALS)
  node = type_info.resolve(node, context.info)
  # This second call allows resolving first-order class attributes.
  node = live_values.resolve(node, context.info, config.PYTHON_LITERALS)
  if is_initial:
    anno.dup(
        node,
        {
            anno.Static.DEFINITIONS: anno.Static.ORIG_DEFINITIONS,
        },
    )
  return node


def apply_(node, context, converter_module):
  """Applies a converter to an AST.

  Args:
    node: ast.AST
    context: converter.EntityContext
    converter_module: converter.Base

  Returns:
    ast.AST, the result of applying converter to node
  """
  node = standard_analysis(node, context)
  node = converter_module.transform(node, context)
  return node
