from hybrid_platform.java_indexer import JavaIndexer, JavaIndexRequest


def test_java_indexer_failure_detail_prefers_stdout_tail() -> None:
    detail = JavaIndexer(JavaIndexRequest(repo_root=".", output_path="index.scip"))._failure_detail(
        stdout="\n".join(
            [
                "[INFO] step 1",
                "[INFO] step 2",
                "[ERROR] BUILD FAILURE",
                "[ERROR] revapi failed",
            ]
        ),
        stderr="Picked up JAVA_TOOL_OPTIONS: -Djava.io.tmpdir=/tmp\n",
        returncode=1,
    )

    assert "BUILD FAILURE" in detail
    assert "revapi failed" in detail
    assert "Picked up JAVA_TOOL_OPTIONS" not in detail
