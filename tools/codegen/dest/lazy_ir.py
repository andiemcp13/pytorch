from typing import List, Union
from dataclasses import dataclass
from tools.codegen.context import method_with_native_function
from tools.codegen.model import (BackendIndex, NativeFunction,
                                 NativeFunctionsGroup)
from tools.codegen.api.types import (BaseCType, OptionalCType, NamedCType,
                                     VectorCType, kernel_signature)
import tools.codegen.api.dispatcher as dispatcher
from tools.codegen.api.lazy import LazyIrSchema, isValueType
from tools.codegen.dest.lazy_ts_lowering import ts_lowering_body

def lazy_value_string_from_optionals(arg: NamedCType) -> str:
    """
    Given a NamedCType from a lazy IR schema,
    generate a c++ string for materializing an rvalue of that arg for passing into
    somewhere that accepts ir::Values (both null and non-null)
    """
    if isValueType(arg.type):
        if isinstance(arg.type, BaseCType):
            return f"lazy_{arg.name}.GetIrValue()"
        elif isinstance(arg.type, OptionalCType):
            return f"lazy_{arg.name} ? " \
                   f"lazy_{arg.name}.GetIrValue() : " \
                   "torch::lazy::Value()"
        else:
            raise AssertionError("TODO not sure if there are other valid types to handle here")

def node_ctor_arg_rvalue_string(arg: NamedCType, schema: LazyIrSchema) -> str:
    """
    Given a NamedCType from a lazy IR schema,
    generate a c++ string for materializing an rvalue of that arg for passing into
    a lazy Node constructor.
    """

    if isValueType(arg.type):
        if isinstance(arg.type, BaseCType):
            if arg.name in schema.wrapped_scalar_names:
                return f"torch::lazy::LazyGraphExecutor::Get()->GetIrValueForScalarFromCodegen({arg.name})"
            return f"lazy_{arg.name}.GetIrValue()"
        elif isinstance(arg.type, OptionalCType):
            if arg.name in schema.wrapped_scalar_names:
                return f"{arg.name} ? " \
                    f"c10::make_optional(torch::lazy::LazyGraphExecutor::Get()->GetIrValueForScalarFromCodegen(*{arg.name})) : " \
                    "c10::nullopt"
            return f"lazy_{arg.name} ? " \
                   f"c10::make_optional(lazy_{arg.name}.GetIrValue()) : " \
                   "c10::nullopt"
        else:
            raise AssertionError("TODO not sure if there are other valid types to handle here")
    else:
        if isinstance(arg.type, VectorCType) and isinstance(arg.type.elem, BaseCType):
            return f"std::vector<{arg.type.elem.type}>({arg.name}.begin(), {arg.name}.end())"
        elif (isinstance(arg.type, OptionalCType) and
                isinstance(arg.type.elem, VectorCType) and
                isinstance(arg.type.elem.elem, BaseCType)):
            return f"torch::lazy::ToOptionalVector<{arg.type.elem.elem.type}>({arg.name})"
        else:
            return f"{arg.name}"

def node_ctor_inputs(schema: LazyIrSchema) -> str:
    """
    Produce a formatted string with the arguments as passed into the constructor of a node class.
    """
    node_ctor_values = [node_ctor_arg_rvalue_string(arg, schema) for arg in schema.filtered_types()] + ["node_hash", "dag_hash"]
    return ",\n                              ".join(node_ctor_values)


def aten_symbol(schema: LazyIrSchema) -> str:
    missing_interned_strings = {
        'sigmoid_backward',
    }
    if schema.aten_name in missing_interned_strings:
        return f'c10::Symbol::fromQualString("aten::{schema.aten_name}")'
    return f'at::aten::{schema.aten_name}'

def cached_shape_inference(func: NativeFunction, schema: LazyIrSchema,
                           returns_length: int, all_types: List[NamedCType]) -> str:
    # call the meta kernel if it exists, to compute output shape/dtype for our IR
    if func.structured or func.structured_delegate is not None:
        meta_out = """cached_shapes = shape_cache->Add(dag_hash, std::make_shared<std::vector<Shape>>(std::initializer_list<Shape>{Shape(out_meta.scalar_type(), out_meta.sizes().vec())}));"""
        if returns_length > 1:
            def this_shape(i: int) -> str:
                return f"Shape(std::get<{i}>(out_meta).scalar_type(), std::get<{i}>(out_meta).sizes().vec())"
            inner_shapes_str = ','.join([this_shape(i) for i in range(returns_length)])
            meta_out = "cached_shapes = shape_cache->Add(dag_hash, std::make_shared<std::vector<Shape>>(std::initializer_list<Shape>{" + inner_shapes_str + "}));"

        shapes_body = f"""
            auto out_meta = at::meta::{schema.aten_name}({', '.join(str(t.name) for t in all_types)});
            {meta_out}
"""
    else:
        shape_sig = ComputeShapeSignature(func)
        shapes_body = f"""
            cached_shapes = shape_cache->Add(dag_hash, std::make_shared<std::vector<Shape>>({shape_sig.shape_call}));
"""    
    
    shapes_str = f"""
        auto shape_cache = torch::lazy::GetShapeCache();
        auto cached_shapes = shape_cache->Get(dag_hash);
        if (cached_shapes == nullptr) {{{shapes_body}
        }}
        auto& shapes = *cached_shapes;
        TORCH_INTERNAL_ASSERT(shapes.size() == {returns_length});
"""
    return shapes_str

@dataclass(frozen=True)
class LazyIR:
    backend_index: BackendIndex
    node_base: str

    @method_with_native_function
    def __call__(self, f: Union[NativeFunctionsGroup, NativeFunction]) -> List[str]:
        func = f.functional.func if isinstance(f, NativeFunctionsGroup) else f.func
        return self.gen(f)

    def gen(self, f: Union[NativeFunctionsGroup, NativeFunction]) -> List[str]:
        # for now, we just want one IR class decl and soon after also the method defs
        # and we use the functional version not out/inplace.
        func = f.functional.func if isinstance(f, NativeFunctionsGroup) else f.func
        schema = LazyIrSchema(func)
        all_types = schema.filtered_types()
        value_types = schema.filtered_types(values=True, scalars=False)
        scalar_types = schema.filtered_types(values=False, scalars=True)

        node_ctor_args = ", ".join([f"const {i.cpp_type()}& {i.name}" for i in all_types] + ["torch::lazy::hash_t node_hash, torch::lazy::hash_t dag_hash"])
        scalar_initializers = ",\n        ".join([f"{t.name}({t.name})" for t in scalar_types])
        comma_if_scalar_initializers = ",\n" if len(scalar_initializers) else ""
        scalar_decls = "\n  ".join([f"{t.cpp_type()} {t.name};" for t in scalar_types])
        base_ctor_value_args_list = []
        optional_values = []
        for t in value_types:
            if isinstance(t.type, BaseCType):
                base_ctor_value_args_list.append(f"{t.name}")
            elif isinstance(t.type, OptionalCType):
                base_ctor_value_args_list.append(f"{t.name}.value_or(kNullValue)")
                optional_values.append(t.name)
            else:
                raise AssertionError("TODO not sure if there are other valid types to handle here")
        base_ctor_value_args = ", ".join(base_ctor_value_args_list)
        has_optional_decls = "\n  ".join([f"bool has_{value}: 1;" for value in optional_values])
        has_optional_defs = "\n    ".join([f"has_{value} = !!{value};" for value in optional_values])
        members_to_string = []
        for t in scalar_types:
            if isinstance(t.type, OptionalCType):
                members_to_string.append(f"""if ({t.name}.has_value()) {{
    ss << ", {t.name}=" << {t.name}.value();
}} else {{
    ss << ", {t.name}=null";
}}""")
            else:
                members_to_string.append(f'ss << ", {t.name}=" << {t.name};')
        members_to_string_str = "\n    ".join(members_to_string)

        return [f"""\
class {schema.node_name} : public {self.node_base} {{
 public:
  {schema.node_name}({node_ctor_args}, std::vector<Shape>&& shapes)
      : {self.node_base}(torch::lazy::OpKind({aten_symbol(schema)}),
              {{{base_ctor_value_args}}}, std::move(shapes),
              /* num_outputs */ {len(func.returns)},
              node_hash,
              dag_hash){comma_if_scalar_initializers}
        {scalar_initializers}

  {{
    {has_optional_defs}
  }}

  std::string ToString() const override {{
    std::stringstream ss;
    ss << TsNode::ToString();
    {members_to_string_str}
    return ss.str();
  }}

  torch::lazy::TSOpVector Lower(std::shared_ptr<torch::jit::GraphFunction> function,
                   torch::lazy::TSLoweringContext* loctx) const override {{
    {ts_lowering_body(f)}
  }}

  {scalar_decls}
  {has_optional_decls}

}};

""", ]


def lazy_tensor_decls(value_types: List[NamedCType], tensor_class: str, schema: LazyIrSchema) -> str:
    lazy_tensor_decls: List[str] = []
    for t in value_types:
        if t.name in schema.wrapped_scalar_names:
            # no lazy tensor wrapper for scalars that are promoted to IR values
            continue
        if isinstance(t.type, BaseCType):
            lazy_tensor_decls.append(
                f"{tensor_class} lazy_{t.name} = "
                f"torch::lazy::GetLtcTensorOrCreateForWrappedNumber({t.name}, *device);")
        elif isinstance(t.type, OptionalCType):
            # TODO(alanwaketan): Maybe we want to apply GetLtcTensorOrCreateForWrappedNumber here, but hold it
            # until we encounter a real world example.
            lazy_tensor_decls.append(
                f"    {tensor_class} lazy_{t.name} = torch::lazy::TryGetLtcTensor({t.name}.value_or(at::Tensor()));")
        else:
            raise AssertionError("TODO not sure if there are other valid types to handle here")
    return "\n    ".join(lazy_tensor_decls)

@dataclass(frozen=True)
class GenLazyNativeFuncDefinition:
    class_method_name: str
    backend_index: BackendIndex
    tensor_class: str

    @method_with_native_function
    def __call__(self, func: NativeFunction) -> List[str]:
        sig = kernel_signature(func, self.backend_index)

        # Lazy IR stuff
        schema = LazyIrSchema(func.func)
        all_types = schema.filtered_types()
        value_types = schema.filtered_types(values=True, scalars=False)
        scalar_types = schema.filtered_types(values=False, scalars=True)
        returns_length = len(schema.returns)

        value_types_names = ", ".join([f"{t.name}" for t in value_types])
        get_device_str = f"""auto device = torch::lazy::GetBackendDevice({value_types_names});"""
        lazy_tensor_decls_str = lazy_tensor_decls(value_types, self.tensor_class, schema)
        node_ctor_input_str = node_ctor_inputs(schema)

        hashes = [f"static_cast<uint32_t>(at::aten::{schema.aten_name})"]
        hashes += [f"{f.name}" for f in scalar_types]
        node_hash = f"torch::lazy::hash_t node_hash = torch::lazy::MHash({', '.join(hashes)});"
        ir_values = [lazy_value_string_from_optionals(v) for v in value_types if v.name not in schema.wrapped_scalar_names]
        dag_hash = f"torch::lazy::hash_t dag_hash = torch::lazy::OperandHashes({{{', '.join(ir_values)}}}, node_hash);"
        hash_str = node_hash + "\n        " + dag_hash

        shape_str = cached_shape_inference(func, schema, returns_length, all_types)

        node_str = f"""auto node = torch::lazy::MakeNode<ir::ops::{schema.node_name}>({node_ctor_input_str},
                                                                                      std::move(shapes));"""

        assert len(value_types) > 0, f"Only supporting tensor ops so far, none found in {sig}"
        first_tensor = value_types[0]
        bridge_str = f"""auto result = torch::lazy::CreateAtenFromLtcTensor(torch::lazy::LazyTensor::Create(std::move(node), lazy_{first_tensor.name}.GetDevice()));"""
        if returns_length > 1:
            bridge_str = f"""std::vector<{self.tensor_class}> lazy_tensors;
        for (int i = 0; i < {returns_length}; i++) {{
            lazy_tensors.push_back(torch::lazy::LazyTensor::Create(torch::lazy::Value(node, i), lazy_{first_tensor.name}.GetDevice()));
        }}
        auto result = torch::lazy::TupleAtenFromLtcTensors<{returns_length}>(lazy_tensors);"""
        if schema.name.name.inplace:
            assert returns_length == 1, "We assumed there was no such case where an op is an in-place variant " \
                                        "and has tuple outputs."
            bridge_str = f"""lazy_{first_tensor.name}.SetInPlaceIrValue(node);
        auto& result = {first_tensor.name};"""


        return [f"""\
    {sig.decl(name=f"{self.class_method_name}::{schema.aten_name}")} {{
        TORCH_LAZY_FN_COUNTER("lazy::");
        {get_device_str}
        {lazy_tensor_decls_str}
        {hash_str}
        {shape_str}
        {node_str}
        {bridge_str}
        return result;
    }};\n
    """]

class ComputeShapeSignature:
    """
    Here we use the base name as the suffix of the signature to avoid generating for in-place variants.
    """
    @method_with_native_function
    def __init__(self, f: NativeFunction):
        self.__schema = LazyIrSchema(f.func)
        self.__dispatch_args = ', '.join([a.decl() for a in dispatcher.arguments(f.func)])
        self.__call_args = ", ".join([f"{t.name}" for t in self.__schema.filtered_types()])

    def __decl_suffix(self) -> str:
        return f"{self.__schema.base_name}({self.__dispatch_args})"

    def __call_suffix(self) -> str:
        return f"{self.__schema.base_name}({self.__call_args})"

    @property
    def shape_decl(self) -> str:
        return f"std::vector<Shape> compute_shape_{self.__decl_suffix()}"

    @property
    def shape_call(self) -> str:
        return f"torch_lazy_tensors::ir::ops::compute_shape_{self.__call_suffix()}"


@dataclass(frozen=True)
class GenLazyShapeInferenceDefinition:
    backend_index: BackendIndex
    tensor_class: str

    @method_with_native_function
    # def gen_lazy_shape_inference_decl(f: NativeFunction, backend_index: BackendIndex, tensor_class: str) -> List[str]:
    def __call__(self, f: NativeFunction) -> List[str]:
        sig = kernel_signature(f, self.backend_index)

        # Lazy IR stuff
        schema = LazyIrSchema(f.func)
        value_types = schema.filtered_types(values=True, scalars=False)
        lazy_tensor_decls_str = lazy_tensor_decls(value_types, self.tensor_class, schema)
        node_ctor_input_str = node_ctor_inputs(schema)

        # Only generate shape/dtype fn for non-structured kernels,
        # since we just use the meta function for structured kernels
        if not f.structured and f.structured_delegate is None:
            shape_sig = ComputeShapeSignature(f)
            return ["\n".join([f"{shape_sig.shape_decl};"])]
        else:
            return []
