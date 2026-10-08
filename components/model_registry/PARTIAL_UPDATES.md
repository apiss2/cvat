# Updating an existing ONNX model

Open a model's detail page and select **このモデルを更新**. Existing metadata is
prefilled. Select only the files to replace; unselected Python code, ONNX weights,
helper Python modules and the sample image are retained from the active revision.
Each existing ONNX file has its own replacement input. Its filename must match
the existing filename exactly, including case. A different name is an error;
the browser does not rename the selected file. Replacement code must be named
`model.py`.

Metadata-only, code-only, sample-only and individual weight updates are supported.
Partial updates retain the weight filename list. They cannot add, remove or
rename weights. New registrations use individual files through the form or
`POST /api/upload`; external ZIP registration and ZIP-based full updates are removed.

The API is `POST /api/models/{model_id}/update` with multipart fields:
`expected_revision` (required), and optional `manifest`, `code`, `weights`,
`sample`. The manifest is a partial JSON object; omitted fields are retained,
explicit empty strings clear optional text, and nested `polygon` settings merge
by key. A supplied `weights` array must equal the active revision's filename list.
Multipart weight filenames identify the existing files to replace. Upload field
`code` replaces `model.py` and must have that filename.

An update creates a separate complete candidate package and runs the existing
sample-inference validation. Internal ZIP assembly, storage validation and inherited
helper Python modules are retained. Only a successful candidate becomes active. Old
revisions are immutable. Ownership checks and expected-revision checks prevent
another user's update or a concurrent update/delete from being overwritten.
Failures and cancellation of validation do not modify the published revision.

```sh
python -m pytest --noconftest components/model_registry/tests/test_partial_updates.py -q
```

The tests exercise assembly and API contracts with a service double. They are not
an actual ONNX worker/container inference test. The registry's existing validation
and publication path remains unchanged and needs its usual integration checks.
