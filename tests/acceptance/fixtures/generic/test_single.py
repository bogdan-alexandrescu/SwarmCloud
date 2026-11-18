"""The one test the generic runner's `pytest` acceptance check names in
`input.paths`: that run must report "1 passed", not the directory's 3."""


def test_only_this_one_ran():
    assert "acceptance".upper() == "ACCEPTANCE"
