"""Cloudinary-backed Django storage. Files are uploaded as private ("authenticated") raw assets,
so they are only reachable through the app's authenticated download endpoints."""
import posixpath
import urllib.request

import cloudinary
import cloudinary.api
import cloudinary.uploader
import cloudinary.utils
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible

DELIVERY_TYPE = "authenticated"
RESOURCE_TYPE = "raw"  # raw keeps any file type (PDF, images, docs) byte-for-byte


@deconstructible
class CloudinaryStorage(Storage):
    def __init__(self, folder=None):
        self.folder = (folder or getattr(settings, "CLOUDINARY_FOLDER", "acc")).strip("/")

    def _public_id(self, name):
        return f"{self.folder}/{name}" if self.folder else name

    def _save(self, name, content):
        content.seek(0) if hasattr(content, "seek") else None
        cloudinary.uploader.upload(
            content, public_id=self._public_id(name), resource_type=RESOURCE_TYPE,
            type=DELIVERY_TYPE, overwrite=False, unique_filename=False, use_filename=False,
        )
        return name

    def _open(self, name, mode="rb"):
        url = self._signed_url(name)
        with urllib.request.urlopen(url, timeout=30) as resp:
            f = ContentFile(resp.read(), name=posixpath.basename(name))
        return f

    def _signed_url(self, name):
        return cloudinary.utils.private_download_url(
            self._public_id(name), "", resource_type=RESOURCE_TYPE, type=DELIVERY_TYPE,
        )

    def delete(self, name):
        cloudinary.uploader.destroy(self._public_id(name), resource_type=RESOURCE_TYPE, type=DELIVERY_TYPE, invalidate=True)

    def exists(self, name):
        try:
            cloudinary.api.resource(self._public_id(name), resource_type=RESOURCE_TYPE, type=DELIVERY_TYPE)
            return True
        except cloudinary.exceptions.NotFound:
            return False

    def size(self, name):
        return cloudinary.api.resource(self._public_id(name), resource_type=RESOURCE_TYPE, type=DELIVERY_TYPE)["bytes"]

    def url(self, name):
        return self._signed_url(name)
