from env_reads import detect_env_reads


def test_detects_environ_subscript_and_getenv_with_scope_and_line():
    changed = {
        "app/api/chargebacks.py": "import os\nX = os.environ['CHARGEBACK_PROVIDER_URL']\n",
        "app/workers/chargeback_worker.py": "import os\nQ = os.getenv('CHARGEBACK_QUEUE_NAME')\n",
    }
    reads = detect_env_reads(changed)
    by_name = {r["name"]: r for r in reads}

    assert set(by_name) == {"CHARGEBACK_PROVIDER_URL", "CHARGEBACK_QUEUE_NAME"}
    assert by_name["CHARGEBACK_PROVIDER_URL"]["scope"] == "api"
    assert by_name["CHARGEBACK_PROVIDER_URL"]["source_file"] == "app/api/chargebacks.py"
    assert by_name["CHARGEBACK_PROVIDER_URL"]["source_line"] == 2
    assert by_name["CHARGEBACK_QUEUE_NAME"]["scope"] == "worker"


def test_same_var_in_api_and_worker_is_scope_both():
    changed = {
        "app/api/x.py": "import os\nos.getenv('SHARED_FLAG')\n",
        "app/workers/y.py": "import os\nos.getenv('SHARED_FLAG')\n",
    }
    reads = detect_env_reads(changed)
    assert len(reads) == 1
    assert reads[0]["name"] == "SHARED_FLAG"
    assert reads[0]["scope"] == "both"


def test_ignores_non_python_and_lowercase_names():
    changed = {
        "README.md": "os.environ['NOT_CODE']",
        "app/api/z.py": "import os\nos.getenv('lower_case')\n",
    }
    assert detect_env_reads(changed) == []


def test_ignores_commented_out_reads():
    changed = {
        "app/api/z.py": (
            "import os\n"
            "# LEGACY = os.getenv('OLD_PROVIDER_URL')\n"
            "ACTIVE = os.getenv('NEW_PROVIDER_URL')  # trailing comment\n"
        ),
    }
    names = {r["name"] for r in detect_env_reads(changed)}
    assert names == {"NEW_PROVIDER_URL"}
