"""Shared helpers for the Del-Fi test suite."""

import unittest


def function_suite(
    module_globals: dict, standard_tests: unittest.TestSuite
) -> unittest.TestSuite:
    """Return *standard_tests* plus every bare ``test_*`` function in a module.

    unittest only discovers ``TestCase`` subclasses. Test modules call this
    from their ``load_tests`` hook, which unittest runs after the module has
    fully imported, so a function is collected wherever it sits in the file.
    """
    suite = unittest.TestSuite(standard_tests)
    for name, obj in list(module_globals.items()):
        if name.startswith("test_") and callable(obj) and not isinstance(obj, type):
            suite.addTest(unittest.FunctionTestCase(obj, description=name))
    return suite
