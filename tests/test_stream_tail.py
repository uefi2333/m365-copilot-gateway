from mcg.substrate.client import fold_stream_text


def test_final_snapshot_flushes_tail_chars():
    answer, emit = fold_stream_text("流式正常", "流式正常结束", final=True)
    assert answer == "流式正常结束"
    assert emit == "结束"


def test_live_divergent_snapshot_does_not_corrupt_stream():
    answer, emit = fold_stream_text("abc", "axcdef")
    assert answer == "axcdef"
    assert emit is None
