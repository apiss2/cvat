# Updating an existing ONNX model

Open a model's detail page and select **このモデルを更新**. Existing metadata is
prefilled. Select only the files to replace; unselected Python code, ONNX weights,
helper Python modules and the sample image are retained from the active revision.
Each existing ONNX file has its own replacement input, so a locally renamed file
can replace its existing logical filename without changing `model.py`.

Metadata-only, code-only, sample-only and individual weight updates are supported.
Changing the number or logical names of weights remains available through a full
ZIP upload or the partial-update API. New-model registration and full ZIP uploads
retain their existing semantics.

The API is `POST /api/models/{model_id}/update` with multipart fields:
`expected_revision` (required), and optional `manifest`, `code`, `weights`,
`sample`. The manifest is a partial JSON object; omitted fields are retained,
explicit empty strings clear optional text, and nested `polygon` settings merge
by key. A supplied `weights` array in the manifest replaces that filename list;
new names require matching uploads. Multipart weight filenames identify the
logical files to replace. Upload field `code` replaces `model.py`.

An update creates a separate complete candidate package and runs the existing
sample-inference validation. Only a successful candidate becomes active. Old
revisions are immutable. Ownership checks and expected-revision checks prevent
another user's update or a concurrent update/delete from being overwritten.
Failures and cancellation of validation do not modify the published revision.

```sh
python -m pytest components/model_registry/tests/test_partial_updates.py -q
```

The tests exercise assembly and API contracts with a service double. They are not
an actual ONNX worker/container inference test. The registry's existing validation
and publication path remains unchanged and needs its usual integration checks.
