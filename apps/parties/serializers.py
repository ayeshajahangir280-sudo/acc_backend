from rest_framework import serializers

from .models import Party


class PartySerializer(serializers.ModelSerializer):
    class Meta:
        model = Party
        fields = ["id", "name", "kind", "email", "phone", "address", "tax_number", "is_active"]
        validators = []  # uniqueness enforced in validate() because organization is not a client field

    def validate(self, attrs):
        org = self.context["organization"]
        qs = Party.objects.filter(organization=org, name=attrs.get("name", getattr(self.instance, "name", None)),
                                  kind=attrs.get("kind", getattr(self.instance, "kind", None)))
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A party with this name and kind already exists.")
        return attrs

    def create(self, validated_data):
        return Party.objects.create(organization=self.context["organization"], **validated_data)
