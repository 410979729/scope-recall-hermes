# probes

`probes/hermes/` is the P11 real-host A2A test kit: it prepares an isolated
TEST Hermes home, starts and stops a TEST gateway, and sends synthetic A2A
requests.  `tests/host/hermes/test_p11_a2a_prepare.py` covers the preparation
step; the operator walkthrough is `docs/p11-a2a-test.zh-CN.md`.

`probes/eval_model_runtime.py` backs the `model_runtime` tier
(`tests/host/test_eval_model_runtime.py`), which only runs with
`--allow-model-calls`.

Nothing under `probes/` ships in the wheel.
