import json
import os
from contextlib import contextmanager
from glob import glob
from unittest.mock import patch

import pytest

from meterelf import _calibration, _debug, _main, _params

mydir = os.path.abspath(os.path.dirname(__file__))
project_dir = os.path.abspath(os.path.join(mydir, os.path.pardir))

params_fn = os.path.join('sample-images1', 'params.yml')

mocks = []


def setup_module():
    mocks.append(patch('cv2.imshow'))
    mocks.append(patch('cv2.waitKey'))
    for mock_func in mocks:
        mock_func.start()


def teardown_module():
    for mock_func in mocks:
        mock_func.stop()


ALLOWED_INACCURACY = 0.00

FILENAMES_OF_EXPECTED_OUTPUT = {
    'sample-images1': 'sample-images1_stdout.txt',
    'sample-images2': 'sample-images2_stdout.txt',
}


@pytest.mark.parametrize('sample_dir', FILENAMES_OF_EXPECTED_OUTPUT.keys())
def test_main_with_all_sample_images(capsys, sample_dir):
    filename_of_expected_output = FILENAMES_OF_EXPECTED_OUTPUT[sample_dir]
    expected_all_output_file = os.path.join(mydir, filename_of_expected_output)
    with open(expected_all_output_file, 'rt') as fp:
        expected_output = fp.read()

    with cwd_as(project_dir):
        old_dir = os.getcwd()
        os.chdir(sample_dir)
        try:
            all_sample_images = sorted(glob('*.jpg'))
            _main.main(['meterelf', 'params.yml'] + all_sample_images)
        finally:
            os.chdir(old_dir)

    captured = capsys.readouterr()

    result = [
        line.split(': ', 1)
        for line in captured.out.splitlines()
    ]
    expected = [
        line.split(': ', 1)
        for line in expected_output.splitlines()
    ]
    (filenames, values) = zip(*result)
    (expected_filenames, expected_values) = zip(*expected)
    value_map = dict(result)
    failed_files = set()

    diffs = []
    for precision in [1000, 0.5, 0.1, 0.05, 0.04, 0.03, 0.02, 0.01, 0.005]:
        for (filename, expected_value) in expected:
            value = value_map.get(filename)
            value_f = to_float(value)
            expected_f = to_float(expected_value)
            line = None
            if value_f is None or expected_f is None:
                if value != expected_value:
                    line = '{:45s}: got: {} | expected: {}'.format(
                        filename, value, expected_value)
            else:
                diff = value_f - expected_f
                if abs(diff) > 900:
                    diff -= 1000
                if abs(diff) >= precision and precision > ALLOWED_INACCURACY:
                    line = '{:42s} {:8.2f} (got: {} | expected: {})'.format(
                        filename, diff, value, expected_value)
            if line is not None and line not in diffs:
                failed_files.add(filename)
                diffs.append(line)
    if diffs:
        diffs.append(
            'Failed {} of {} files'.format(len(failed_files), len(filenames)))
    assert '\n'.join(diffs) == ''

    assert captured.err == ''


def to_float(x):
    if x is None:
        return None
    try:
        return float(x)
    except ValueError:
        return None


@contextmanager
def cwd_as(directory):
    old_dir = os.getcwd()
    os.chdir(directory)
    try:
        yield
    finally:
        os.chdir(old_dir)


@pytest.mark.parametrize('mode', ['normal', 'debug'])
def test_find_dial_centers(mode):
    debug_value = {'masks'} if mode == 'debug' else {}
    params = _params.load(params_fn)
    files = _calibration.get_image_filenames(params)
    with patch.object(_debug, 'DEBUG', new=debug_value):
        result = _calibration.find_dial_centers(params, files)
    assert len(result) == 4
    sorted_result = sorted(result, key=(lambda x: x.center[0]))

    for (center_data, expected) in zip(result, EXPECTED_CENTER_DATA):
        (expected_x, expected_y, expected_d) = expected
        coords = center_data.center
        diameter = center_data.diameter
        assert diameter == expected_d
        assert abs(coords[0] - expected_x) < 0.05
        assert abs(coords[1] - expected_y) < 0.05

    assert result == sorted_result


EXPECTED_CENTER_DATA = [
    (37.4, 63.5, 14),
    (94.5, 86.3, 15),
    (135.6, 71.5, 13),
    (161.0, 36.5, 13),
]


@pytest.mark.parametrize('filename', [
    '20180814021309-01-e01.jpg',
    '20180814021310-00-e02.jpg',
])
def test_raises_on_debug_mode(capsys, filename):
    error_msg = EXPECTED_ERRORS[filename]
    image_path = os.path.join(project_dir, 'sample-images1', filename)
    with patch.object(_debug, 'DEBUG', new={'1'}):
        with cwd_as(project_dir):
            with pytest.raises(Exception) as excinfo:
                _main.main(['meterelf', params_fn] + [image_path])
            assert excinfo.value.get_message() == error_msg
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == ''


EXPECTED_ERRORS = {
    '20180814021309-01-e01.jpg': 'Dials not found (match val = 0.0)',
    '20180814021310-00-e02.jpg': 'Dials not found (match val = 17495704.0)',
}


def test_output_in_debug_mode(capsys):
    filename = '20180814215230-01-e136.jpg'
    image_path = os.path.join(project_dir, 'sample-images1', filename)
    with patch.object(_debug, 'DEBUG', new={'1'}):
        with cwd_as(project_dir):
            _main.main(['meterelf', params_fn] + [image_path])
    captured = capsys.readouterr()
    basic_data = image_path + ': 253.623'
    assert captured.out.startswith(basic_data)
    debug_data_str = captured.out[len(basic_data):].replace("'", '"').strip()
    debug_data = json.loads(debug_data_str)
    assert isinstance(debug_data, dict)
    assert set(debug_data) == {'0.0001', '0.001', '0.01', '0.1', 'value'}
    assert abs(debug_data['0.0001'] - 6.23) < 0.005
    assert abs(debug_data['0.001'] - 3.3) < 0.05
    assert abs(debug_data['0.01'] - 5.1) < 0.05
    assert abs(debug_data['0.1'] - 2.4) < 0.05
    assert abs(debug_data['value'] - 253.62306) < 0.000005
    assert captured.err == ''
