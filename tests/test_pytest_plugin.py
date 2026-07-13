def test_pytest_plugin_records_then_replays(pytester, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    call_file = pytester.path / "calls.txt"
    monkeypatch.setenv("CALL_FILE", str(call_file))
    pytester.makeini(
        """
        [pytest]
        pollard_recordings_dir = recordings
        """
    )
    pytester.makepyfile(
        test_agent="""
        import os
        from pathlib import Path

        def test_agent_step(pollard_run):
            def fn(_payload):
                path = Path(os.environ["CALL_FILE"])
                path.write_text(path.read_text() + "x" if path.exists() else "x")
                return {"text": "ok", "usage": {"input_tokens": 2, "output_tokens": 3}}

            node = pollard_run.model_call({"model": "mock-1"}, fn=fn)
            assert node.result["text"] == "ok"
        """
    )

    result = pytester.runpytest(
        "-p",
        "no:pollard",
        "-p",
        "pollard.pytest_plugin",
        "--pollard-mode=record",
        "-q",
    )
    result.assert_outcomes(passed=1)
    assert call_file.read_text() == "x"
    assert (pytester.path / "recordings" / "test_agent.db").exists()

    result = pytester.runpytest(
        "-p",
        "no:pollard",
        "-p",
        "pollard.pytest_plugin",
        "--pollard-mode=replay",
        "-q",
    )
    result.assert_outcomes(passed=1)
    assert call_file.read_text() == "x"
