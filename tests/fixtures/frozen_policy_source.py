"""A hashed training request and source file for synthetic historical policy tests."""

from quanto.learning.registry import hash_files
from quanto.objects import digest, json_bytes


def attach_frozen_policy_input(store, root, job_id, dataset_id, spec, model_id):
    source = root / "job-inputs" / job_id
    source.mkdir(parents=True)
    (source / "model_spec.json").write_bytes(json_bytes(spec))
    frozen = {"input": {"request": {"dataset_id": dataset_id, "model_id": model_id,
                                    "spec": spec}},
              "source_hashes": hash_files(source)}
    input_path = source / "input.json"
    input_path.write_bytes(json_bytes(frozen))
    with store.transaction() as db:
        db.execute("UPDATE jobs SET input_path=? WHERE id=?", (str(input_path), job_id))
        db.execute("INSERT INTO learning_submissions VALUES (?,?)", (job_id, digest(input_path.read_bytes())))
    return input_path
