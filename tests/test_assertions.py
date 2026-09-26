from jev_test_triage.assertions import assertion_lines, features


def test_python_loose_only():
    code = "def test_x():\n    r = f()\n    assert r is not None\n    assert isinstance(r, dict)\n    assert len(r) > 0\n"
    f = features(code, "python")
    assert (f["n_assert"], f["n_loose"], f["n_exact"]) == (3, 3, 0)
    assert f["loose_only"] == 1.0


def test_python_exact_and_raises():
    code = "def test_x():\n    assert f(2) == 4\n    with pytest.raises(ValueError):\n        f(-1)\n"
    f = features(code, "python")
    assert (f["n_exact"], f["has_raises"], f["loose_only"]) == (2, 1.0, 0.0)


def test_python_mock_only():
    code = "def test_x():\n    m = Mock()\n    run(m)\n    m.send.assert_called_once_with('a')\n"
    f = features(code, "python")
    assert (f["n_mock"], f["mock_only"]) == (1, 1.0)


def test_js_matchers():
    code = """it('x', () => {
      expect(a).toBeDefined()
      expect(b).toEqual({ id: 1 })
      expect(spy).toHaveBeenCalledWith('x')
      expect(() => f()).toThrow()
      expect(c).not.toBeNull()
    })"""
    f = features(code, "typescript")
    assert (f["n_loose"], f["n_exact"], f["n_mock"]) == (3, 1, 1)


def test_no_assertion():
    assert features("def test_x():\n    f()\n", "python")["no_assertion"] == 1.0


def test_assertion_lines():
    code = "it('x', () => {\n  const r = f()\n  expect(r).toBe(1)\n})"
    assert assertion_lines(code, "typescript") == ["expect(r).toBe(1)"]
