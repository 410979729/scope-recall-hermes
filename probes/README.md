# probes

`probes/hermes/` is the P11 real-host A2A test kit: it prepares an isolated
TEST Hermes home, starts and stops a TEST gateway, and sends synthetic A2A
requests.  `tests/host/hermes/test_a2a_prepare.py` covers the preparation
step.  Run each script with the Hermes runtime's interpreter, from the
repository root:

- `p11_prepare_a2a_test.py` builds the TEST Hermes home, its store, runtime
  config, plugin wrapper and a receipt; it stops, killing nothing, when port
  19921 or 29991 is taken;
- `p11_start_a2a_test.py --duration-seconds 600` starts the TEST gateway (A2A
  on 127.0.0.1:19921) and the metered model bridge (127.0.0.1:29991);
- `p11_request_a2a_test.py` is a dry run; with `--run` it sends one A2A
  request, within the shared ledger's caps and this batch's;
- `p11_stop_a2a_test.py --wait` stops what the starter started.

`probes/eval_model_runtime.py` backs the `model_runtime` tier
(`tests/host/test_eval_model_runtime.py`), which only runs with
`--allow-model-calls`.

Nothing under `probes/` ships in the wheel.
