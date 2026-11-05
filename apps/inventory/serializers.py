from rest_framework import serializers

from .models import Item, Location, ProductionInput, ProductionOrder, StockMovement


def check_org(org, **objs):
    for name, obj in objs.items():
        if obj is not None and obj.organization_id != org.id:
            raise serializers.ValidationError({name: "Not found."})


class ItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = Item
        fields = ["id", "sku", "name", "kind", "unit", "is_active"]
        validators = []

    def validate_sku(self, v):
        qs = Item.objects.filter(organization=self.context["organization"], sku=v)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("SKU already exists.")
        return v

    def validate_kind(self, v):
        if self.instance and self.instance.kind != v and self.instance.movements.exists():
            raise serializers.ValidationError("Kind cannot change once the item has stock movements.")
        return v

    def create(self, data):
        return Item.objects.create(organization=self.context["organization"], **data)


class LocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Location
        fields = ["id", "name", "kind", "is_active"]
        read_only_fields = ["kind"]
        validators = []

    def validate_name(self, v):
        qs = Location.objects.filter(organization=self.context["organization"], name=v)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A location with this name already exists.")
        return v

    def create(self, data):
        return Location.objects.create(organization=self.context["organization"], kind=Location.Kind.WAREHOUSE, **data)


class MovementSerializer(serializers.ModelSerializer):
    sku = serializers.CharField(source="item.sku", read_only=True)
    item_name = serializers.CharField(source="item.name", read_only=True)
    location_name = serializers.CharField(source="location.name", read_only=True)

    class Meta:
        model = StockMovement
        fields = ["id", "date", "sku", "item_name", "location_name", "quantity", "type", "ref_type", "ref_id", "note"]


class AdjustmentSerializer(serializers.Serializer):
    item = serializers.PrimaryKeyRelatedField(queryset=Item.objects.all())
    location = serializers.PrimaryKeyRelatedField(queryset=Location.objects.all(), required=False, allow_null=True)
    quantity = serializers.DecimalField(max_digits=18, decimal_places=4)
    date = serializers.DateField()
    type = serializers.ChoiceField(choices=["opening", "adjustment"], default="adjustment")
    note = serializers.CharField(max_length=300, required=False, allow_blank=True, default="")
    unit_cost = serializers.DecimalField(max_digits=18, decimal_places=4, required=False, allow_null=True, min_value=0)

    def validate(self, a):
        check_org(self.context["organization"], item=a["item"], location=a.get("location"))
        return a


class ProductionInputSerializer(serializers.ModelSerializer):
    item = serializers.PrimaryKeyRelatedField(queryset=Item.objects.all())
    sku = serializers.CharField(source="item.sku", read_only=True)

    class Meta:
        model = ProductionInput
        fields = ["item", "sku", "quantity"]


class ProductionListSerializer(serializers.ModelSerializer):
    sku = serializers.CharField(source="item.sku", read_only=True)
    item_name = serializers.CharField(source="item.name", read_only=True)
    location_name = serializers.CharField(source="location.name", read_only=True)

    class Meta:
        model = ProductionOrder
        fields = ["id", "number", "date", "item", "sku", "item_name", "quantity", "location", "location_name", "status", "note"]


class ProductionSerializer(ProductionListSerializer):
    inputs = ProductionInputSerializer(many=True)
    item = serializers.PrimaryKeyRelatedField(queryset=Item.objects.all())
    location = serializers.PrimaryKeyRelatedField(queryset=Location.objects.all(), required=False, allow_null=True)
    idempotency_key = serializers.CharField(max_length=64, required=False, allow_null=True, write_only=True)

    class Meta(ProductionListSerializer.Meta):
        fields = ProductionListSerializer.Meta.fields + ["inputs", "idempotency_key"]
        read_only_fields = ["number", "status"]

    def validate(self, a):
        org = self.context["organization"]
        check_org(org, item=a["item"], location=a.get("location"))
        for i in a["inputs"]:
            check_org(org, inputs=i["item"])
        return a
