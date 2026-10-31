from rest_framework import serializers

from .models import Branch, Membership, Organization


class OrganizationSerializer(serializers.ModelSerializer):
    role = serializers.SerializerMethodField()

    class Meta:
        model = Organization
        fields = ["id", "name", "legal_name", "base_currency", "fiscal_year_start_month", "is_active", "role"]
        read_only_fields = ["is_active"]

    def get_role(self, obj):
        user = self.context["request"].user
        return next((m.role for m in obj.memberships.all() if m.user_id == user.id), None)

    def validate_fiscal_year_start_month(self, v):
        if not 1 <= v <= 12:
            raise serializers.ValidationError("Must be 1-12.")
        return v


class BranchSerializer(serializers.ModelSerializer):
    class Meta:
        model = Branch
        fields = ["id", "name", "code", "is_active"]

    def create(self, validated_data):
        return Branch.objects.create(organization=self.context["organization"], **validated_data)


class MembershipSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True)

    class Meta:
        model = Membership
        fields = ["id", "user", "username", "role"]
