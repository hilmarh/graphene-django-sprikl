import binascii
from collections import OrderedDict

from django.utils.translation import gettext_lazy as _

import graphene
from graphene_django.rest_framework.serializer_converter import (
    get_graphene_type_from_serializer_field,
)
from graphql_relay import from_global_id
from rest_framework import serializers

from .registry import get_global_registry


class SerializerDjangoObjectTypeField(serializers.ReadOnlyField):
    def __init__(self, object_type, **kwargs):
        self.object_type = object_type
        if "source" not in kwargs:
            kwargs["source"] = "*"
        super(SerializerDjangoObjectTypeField, self).__init__(**kwargs)


class SerializerRelayIDField(serializers.CharField):
    default_error_messages = {
        "invalid_id": _("Not a valid ID."),
        "not_found": _("No {type} matches the given query."),
    }

    def __init__(self, object_type=None, method_name=None, node_class=None, **kwargs):
        self.object_type = object_type
        self.method_name = method_name
        self.node_class = node_class if node_class else graphene.relay.Node

        if node_class and not issubclass(node_class, graphene.relay.Node):
            raise Exception("node_class must be a subclass of relay.Node")

        if not self.object_type and not self.method_name:
            raise Exception("method_name must be passed if object_type is missing")

        kwargs["write_only"] = True
        super(SerializerRelayIDField, self).__init__(**kwargs)

    def run_validation(self, data=serializers.empty):
        value = super(SerializerRelayIDField, self).run_validation(data)

        if value:
            info = self.parent.context.get("info")
            node_class = self.node_class

            try:
                _type, _id = from_global_id(value)
                graphene_type = info.schema.get_type(_type).graphene_type
            except (binascii.Error, UnicodeDecodeError):
                self.fail("invalid_id")

            if not _type or not _id:
                self.fail("invalid_id")

            if self.object_type and graphene_type != self.object_type:
                raise serializers.ValidationError(
                    _("Must receive a %(type)s ID.")
                    % {"type": self.object_type._meta.name}
                )

            if self.method_name:
                method = getattr(self.parent, self.method_name)
                return method(value=value, object_type=graphene_type, object_id=_id)
            else:
                # We make sure the ObjectType implements the "Node" interface
                if node_class not in graphene_type._meta.interfaces:
                    self.fail(
                        "not_found", type=graphene_type._meta.model._meta.object_name
                    )

                get_node = getattr(graphene_type, "get_node", None)
                if get_node:
                    instance = get_node(info, _id)

                    if not instance:
                        self.fail(
                            "not_found",
                            type=graphene_type._meta.model._meta.object_name,
                        )

                    return instance

        return None


def convert_serializer_field(field, registry, is_input=True, is_partial=False, convert_choices_to_enum=True):
    """
    Converts a django rest frameworks field to a graphql field
    and marks the field as required if we are creating an input type
    and the field itself is required
    """
    if isinstance(field, serializers.ChoiceField) and not convert_choices_to_enum:
        graphql_type = graphene.String
    else:
        graphql_type = get_graphene_type_from_serializer_field(field)

    if is_input and field.read_only:
        return None

    if not is_input and field.write_only:
        return None

    args = []
    kwargs = {
        "description": field.help_text,
        "required": is_input and field.required and not is_partial,
    }

    if registry is None:
        registry = get_global_registry()

    # if it is a tuple or a list it means that we are returning
    # the graphql type and the child type
    if isinstance(graphql_type, (list, tuple)):
        kwargs["of_type"] = graphql_type[1]
        graphql_type = graphql_type[0]

    if isinstance(field, serializers.ModelSerializer):
        if is_input:
            graphql_type = convert_serializer_to_input_type(field.__class__, registry)
        else:
            field_model = field.Meta.model
            args = [registry.get_type_for_model(field_model)]
    elif isinstance(field, serializers.Serializer):
        if is_input:
            graphql_type = convert_serializer_to_input_type(field.__class__, registry)
        else:
            raise ValueError("Only ModelSerializer cannot be treated as an input.")
    elif isinstance(field, SerializerDjangoObjectTypeField):
        if is_input:
            raise ValueError(
                "SerializerDjangoObjectTypeField cannot be treated as an input."
            )
        else:
            args = [field.object_type]
    elif isinstance(field, serializers.ListSerializer):
        field = field.child
        if is_input:
            kwargs["of_type"] = convert_serializer_to_input_type(
                field.__class__, registry
            )
        else:
            del kwargs["of_type"]
            field_model = field.Meta.model
            args = [registry.get_type_for_model(field_model)]

    return graphql_type(*args, **kwargs)


def convert_serializer_to_input_type(serializer_class, registry):
    if registry is not None and hasattr(registry, 'get_converted_serializer'):
        converted = registry.get_converted_serializer(serializer_class)
        if converted:
            return converted

    serializer = serializer_class()

    items = {}

    for name, field in serializer.fields.items():
        converted_field = convert_serializer_field(field, registry)

        if converted_field:
            items[name] = converted_field

    if hasattr(serializer, 'Meta') and hasattr(serializer.Meta, "input_name"):
        input_name = serializer.Meta.input_name
    else:
        input_name = "{}Input".format(serializer.__class__.__name__)

    converted = type(input_name, (graphene.InputObjectType,), items)

    if registry is not None and hasattr(registry, 'register_converted_serializer'):
        registry.register_converted_serializer(serializer_class, converted)

    return converted


def fields_for_serializer(
    serializer,
    only_fields,
    exclude_fields,
    registry,
    is_input=False,
    is_update=False,
    is_partial=False,
    convert_choices_to_enum=True,
):
    fields = OrderedDict()

    for name, field in serializer.fields.items():
        is_not_in_only = only_fields and name not in only_fields
        is_excluded = any(
            [
                name in exclude_fields,
                field.write_only
                and not is_input,  # don't show write_only fields in Query
                field.read_only and is_input,  # don't show read_only fields in Input
            ]
        )

        if is_not_in_only or is_excluded:
            continue

        converted_field = convert_serializer_field(
            field, registry, is_input=is_input, is_partial=is_partial, convert_choices_to_enum=convert_choices_to_enum
        )

        if converted_field:
            if is_update and is_input and name == "id":
                raise Exception(
                    "Invalid SerializerUpdateMutation, serializer_class can only have a read_only id field."
                )
            fields[name] = converted_field
    return fields


@get_graphene_type_from_serializer_field.register(SerializerDjangoObjectTypeField)
def convert_serializer_field_to_field(field):
    return graphene.Field


@get_graphene_type_from_serializer_field.register(serializers.Serializer)
def convert_serializer_to_field(field):
    return graphene.Field


@get_graphene_type_from_serializer_field.register(serializers.NullBooleanField)
def convert_serializer_field_to_null_bool(field):
    return graphene.Boolean


@get_graphene_type_from_serializer_field.register(SerializerRelayIDField)
def convert_serializer_relay_id_field_to_id(field):
    return graphene.ID
