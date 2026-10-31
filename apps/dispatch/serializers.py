from rest_framework import serializers

from apps.inventory.models import Item, Location

from .models import VanLoad, VanLoadLine, Vehicle


class VehicleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Vehicle
        fields = ["id", "name", "plate_number", "driver_name", "location", "is_active"]
        read_only_fields = ["location"]
        validators = []

    def validate_name(self, v):
        qs = Vehicle.objects.filter(organization=self.context["organization"], name=v)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A vehicle with this name already exists.")
        return v


class LineSerializer(serializers.ModelSerializer):
    item = serializers.PrimaryKeyRelatedField(queryset=Item.objects.all())
    sku = serializers.CharField(source="item.sku", read_only=True)
    item_name = serializers.CharField(source="item.name", read_only=True)
    quantity = serializers.DecimalField(max_digits=18, decimal_places=4, write_only=True, required=False)
    remaining = serializers.SerializerMethodField()

    class Meta:
        model = VanLoadLine
        fields = ["item", "sku", "item_name", "quantity", "quantity_loaded", "quantity_delivered", "quantity_returned", "remaining"]
        read_only_fields = ["quantity_loaded", "quantity_delivered", "quantity_returned"]

    def get_remaining(self, obj):
        return obj.quantity_loaded - obj.quantity_delivered - obj.quantity_returned if obj.load.status == "closed" else obj.quantity_loaded


class LoadListSerializer(serializers.ModelSerializer):
    vehicle_name = serializers.CharField(source="vehicle.name", read_only=True)

    class Meta:
        model = VanLoad
        fields = ["id", "number", "vehicle", "vehicle_name", "assigned_to", "dispatch_at", "status", "note", "closed_at"]


class LoadSerializer(LoadListSerializer):
    lines = LineSerializer(many=True)
    vehicle = serializers.PrimaryKeyRelatedField(queryset=Vehicle.objects.all())
    source_location = serializers.PrimaryKeyRelatedField(queryset=Location.objects.all(), required=False, allow_null=True)
    idempotency_key = serializers.CharField(max_length=64, required=False, allow_null=True, write_only=True)

    class Meta(LoadListSerializer.Meta):
        fields = LoadListSerializer.Meta.fields + ["lines", "source_location", "idempotency_key"]
        read_only_fields = ["number", "status", "closed_at"]

    def validate(self, a):
        org = self.context["organization"]
        for obj in [a["vehicle"], a.get("source_location"), *[l["item"] for l in a["lines"]]]:
            if obj is not None and obj.organization_id != org.id:
                raise serializers.ValidationError("Referenced vehicle, location or item not found.")
        if any("quantity" not in l for l in a["lines"]):
            raise serializers.ValidationError({"lines": "Each line needs a quantity."})
        return a


class OutcomeSerializer(serializers.Serializer):
    item = serializers.PrimaryKeyRelatedField(queryset=Item.objects.all())
    delivered = serializers.DecimalField(max_digits=18, decimal_places=4, default=0)
    returned = serializers.DecimalField(max_digits=18, decimal_places=4, default=0)


class CloseSerializer(serializers.Serializer):
    lines = OutcomeSerializer(many=True)
