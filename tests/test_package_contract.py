from importlib.metadata import metadata


def test_installed_python_support():
    package = metadata("pdcst")
    assert package["Requires-Python"] == ">=3.10"
    classifiers = package.get_all("Classifier")
    assert "Programming Language :: Python :: 3.10" in classifiers
    assert "Programming Language :: Python :: 3.13" in classifiers
    assert "Programming Language :: Python :: 3.9" not in classifiers
