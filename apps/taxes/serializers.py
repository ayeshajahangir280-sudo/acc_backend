from rest_framework import serializers

from .models import TaxRate


class TaxRateSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaxRate
        fields = ["id", "name", "rate", "is_default", "is_active"]
        validators = []

    def validate_name(self, v):
        qs = TaxRate.objects.filter(organization=self.context["organization"], name=v)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A tax rate with this name already exists.")
        return v

    def validate_rate(self, v):
        if not 0 <= v <= 100:
            raise serializers.ValidationError("Rate must be between 0 and 100.")
        return v

    @staticmethod
    def _single_default(obj):
        if obj.is_default:
            TaxRate.objects.filter(organization=obj.organization, is_default=True).exclude(pk=obj.pk).update(is_default=False)

    def create(self, data):
        obj = TaxRate.objects.create(organization=self.context["organization"], **data)
        self._single_default(obj)
        return obj

    def update(self, obj, data):
        obj = super().update(obj, data)
        self._single_default(obj)
        return obj
