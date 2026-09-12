import unittest

from automatic_stratagems.bounded_json import loads_bounded_json


class BoundedJsonTests(unittest.TestCase):
    def test_non_text_values_are_rejected_without_bytes_conversion(self):
        class ExplosiveBytes:
            called = False

            def __bytes__(self):
                self.called = True
                raise AssertionError('bytes conversion ran')

        explosive = ExplosiveBytes()
        for value in (10 ** 30, explosive):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(ValueError):
                    loads_bounded_json(value, max_bytes=64)
        self.assertFalse(explosive.called)


if __name__ == '__main__':
    unittest.main()
