# -*- coding: utf-8 -*-
# Copyright (C) 2012 Sebastian Wiesner <lunaryorn@googlemail.com>

# This library is free software; you can redistribute it and/or modify it
# under the terms of the GNU Lesser General Public License as published by the
# Free Software Foundation; either version 2.1 of the License, or (at your
# option) any later version.

# This library is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public License
# for more details.

# You should have received a copy of the GNU Lesser General Public License
# along with this library; if not, write to the Free Software Foundation,
# Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301 USA

"""
    tests.plugins.udev_database
    ===========================

    Provide access the udev device database.

    This plugin parses the udev device database from :program:`udevadm` and
    attaches it to the test configuration object.

    .. moduleauthor::  Sebastian Wiesner  <lunaryorn@googlemail.com>
"""

from __future__ import (print_function, division, unicode_literals,
                        absolute_import)

import sys
import os
import re
import errno
import random
import subprocess
from collections import Iterable, Sized

import pytest


class UDevAdm(object):
    """
    Wrap ``udevadm`` utility.
    """

    CANDIDATES = ['/sbin/udevadm', 'udevadm']

    @classmethod
    def find(cls):
        for candidate in cls.CANDIDATES:
            try:
                udevadm = cls(candidate)
                udevadm.read_version()
                return udevadm
            except EnvironmentError as error:
                if error.errno != errno.ENOENT:
                    raise

    def __init__(self, udevadm):
        """
        Create a new ``udevadm`` wrapper for the given udevadm executable.

        ``udevadm`` is the path to udevadm as string.  If relative, ``udevadm`` is
        looked up in ``$PATH``.
        """
        self.udevadm = udevadm

    def read_version(self):
        return int(self._execute('--version'))

    def _execute(self, *args):
        command = [self.udevadm] + list(args)
        proc = subprocess.Popen(command, stdout=subprocess.PIPE)
        output = proc.communicate()[0].strip()
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, command)
        return output

    def read_devices(self):
        database = self._execute('info', '--export-db').splitlines()
        devices = {}
        current_properties = None
        for line in database:
            line = line.strip().decode(sys.getfilesystemencoding())
            if not line:
                continue
            type, value = line.split(': ', 1)
            if type == 'P':
                current_properties = devices.setdefault(value, {})
            elif type == 'E':
                property, value = value.split('=', 1)
                current_properties[property] = value
        return devices

    def read_device_attributes(self, device_path):
        output = self._execute('info', '--attribute-walk',
                               '--path', device_path)
        attribute_dump = output.splitlines()
        attributes = {}
        for line in attribute_dump:
            line = line.strip().decode(sys.getfilesystemencoding())
            if line.startswith('looking at parent device'):
                # we don't continue with attributes of parent devices, we only
                # want the attributes of the given device
                break
            if line.startswith('ATTR'):
                name, value = line.split('==', 1)
                # remove quotation marks from attribute value
                value = value[1:-1]
                # remove prefix from attribute name
                name = re.search('{(.*)}', name).group(1)
                attributes[name] = value
        return attributes

    def query_device(self, device_path, query_type):
        if query_type not in ('symlink', 'name'):
            raise ValueError(query_type)
        try:
            output = self._execute('info', '--root', '--path', device_path,
                                   '--query', query_type)
        except subprocess.CalledProcessError:
            return None
        else:
            return output.decode(sys.getfilesystemencoding())


def _filter_dict(d, blacklisted_attributes):
    return dict((k, v) for k, v in d.items()
                if k not in blacklisted_attributes)


class DeviceData(object):
    """
    Data for a single device.
    """

    # these are volatile, frequently changing properties and attributes, which
    # lead to bogus failures during tests, and therefore they are masked and
    # shall be ignored for test runs.
    PROPERTIES_BLACKLIST = frozenset(
        ['POWER_SUPPLY_CURRENT_NOW', 'POWER_SUPPLY_VOLTAGE_NOW',
         'POWER_SUPPLY_CHARGE_NOW'])

    ATTRIBUTES_BLACKLIST = frozenset(
        ['power_on_acct', 'temp1_input', 'charge_now', 'current_now',
         'urbnum'])

    def __init__(self, device_path, properties, udevadm):
        self.device_path = device_path
        self._all_properties = properties
        self._udevadm = udevadm

    def __repr__(self):
        return '{0}({1})'.format(self.__class__.__name__, self.device_path)

    @property
    def sys_path(self):
        """
        Get the ``sysfs`` path of the device.
        """
        return '/sys' + self.device_path

    @property
    def properties(self):
        """
        Get the device properties as mapping of property names as strings to
        property values as strings.
        """
        return _filter_dict(self._all_properties, self.PROPERTIES_BLACKLIST)

    @property
    def attributes(self):
        """
        Get the device attributes as mapping of attributes names as strings to
        property values as byte strings.

        .. warning::

           As ``udevadm`` only exports printable attributes, this list can be
           incomplete!  Do *not* compare this dictionary directly to a
           attributes dictionary received through pyudev!
        """
        attrs = self._udevadm.read_device_attributes(self.device_path)
        return _filter_dict(attrs, self.ATTRIBUTES_BLACKLIST)

    @property
    def tags(self):
        """
        Get the device tags as list of strings.
        """
        tags = self._all_properties.get('TAGS', '').split(':')
        return [t for t in tags if t]

    @property
    def device_node(self):
        """
        Get the device node path as string, or ``None`` if the device has no
        device node.
        """
        return self._udevadm.query_device(self.device_path, 'name')

    @property
    def device_links(self):
        """
        Get the device links as list of strings.
        """
        links = self._udevadm.query_device(self.device_path, 'symlink')
        return links.split() if links else []

    @property
    def device_number(self):
        """
        Get the device number as integer or 0 if the device has no device
        number.
        """
        if self.device_node:
            return os.stat(self.device_node).st_rdev
        else:
            return 0


class DeviceDatabase(Iterable, Sized):
    """
    The udev device database.

    This class is an iterable over :class:`DeviceData` objects that contain the
    data associated with each device stored in the udev database.
    """

    def __init__(self, udevadm):
        self._udevadm = udevadm
        self._devices = self._udevadm.read_devices()

    def __iter__(self):
        return (DeviceData(d, p, self._udevadm)
                for d, p in self._devices.items())

    def __len__(self):
        return len(self._devices)

    def find_device_data(self, device_path):
        """
        Find device data for the device designated by ``device_path``.

        ``device_path`` is a string containing the device path.

        Return a :class:`DeviceData` object containing the data for the given
        device, or ``None`` if the device was not found.
        """
        try:
            properties = self._devices[device_path]
            return DeviceData(device_path, properties, self._udevadm)
        except KeyError:
            return None


def _get_device_sample(config):
    """
    Compute a sample of the udev device database based on the given pytest
    ``config``.
    """
    if config.option.device is not None:
        device_data = config.udev_database.find_device_data(
            config.option.device)
        if not device_data:
            raise ValueError('{0} does not exist'.format(
                config.option.device))
        return [device_data]
    if config.option.all_devices:
        return list(config.udev_database)
    else:
        device_sample_size = config.getvalue('device_sample_size')
        actual_size = min(device_sample_size, len(config.udev_database))
        return random.sample(list(config.udev_database), actual_size)


def pytest_runtest_setup(item):
    """
    Evaluate the ``udev_version`` marker before running a test.
    """
    marker = getattr(item.obj, 'udev_version', None)
    if marker is not None:
        version_spec = marker.args[0]
        actual_version = item.config.udev_version
        if not eval('{0} {1}'.format(actual_version, version_spec)):
            pytest.skip('udev version mismatch: {0} required, {1} found'.format(
                version_spec, actual_version))


def pytest_addoption(parser):
    parser.addoption('--all-devices', action='store_true',
                     help='Run device tests against *all* devices in the '
                     'database.  By default, only a random sample will be '
                     'checked.', default=False)
    parser.addoption('--device', metavar='DEVICE',
                     help='Run the device tests only against the given '
                     'DEVICE', default=None)
    parser.addoption('--device-sample-size', type='int', metavar='N',
                     help='Use a random sample of N elements (default: 10)',
                     default=10)


def pytest_configure(config):
    # register a marker for required udev versions
    config.addinivalue_line(
        'markers', 'udev_version(spec): mark test to run only if the udev '
        'version matches the given version spec')

    udevadm = UDevAdm.find()
    config.udev_version = udevadm.read_version()
    config.udev_database = DeviceDatabase(udevadm)
    config.udev_device_sample = _get_device_sample(config)


def pytest_funcarg__udev_database(request):
    """
    The udev database as provided by :attr:`pytest.config.udev_database`.
    """
    return request.config.udev_database


