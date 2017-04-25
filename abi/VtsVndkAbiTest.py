#!/usr/bin/env python
#
# Copyright (C) 2017 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import logging
import os
import shutil
import tempfile

from vts.runners.host import asserts
from vts.runners.host import base_test
from vts.runners.host import const
from vts.runners.host import keys
from vts.runners.host import test_runner
from vts.runners.host import utils
from vts.utils.python.controllers import android_device
from vts.utils.python.library import elf_parser
from vts.utils.python.library import vtable_parser


class VtsVndkAbiTest(base_test.BaseTestClass):
    """A test module to verify ABI compliance of vendor libraries.

    Attributes:
        _dut: the AndroidDevice under test.
        _temp_dir: The temporary directory for libraries copied from device.
        _vendor_lib: The directory to which /vendor/lib is copied.
        _vendor_lib64: The directory to which /vendor/lib64 is copied.
        _system_lib: The directory to which /system/lib is copied.
        _system_lib64: The directory to which /system/lib64 is copied.
        _sdk_version: String, the SDK version supported by the device.
        data_file_path: The path to VTS data directory.
    """
    _TARGET_VENDOR_LIB = "/vendor/lib"
    _TARGET_VENDOR_LIB64 = "/vendor/lib64"
    _TARGET_SYSTEM_LIB = "/system/lib"
    _TARGET_SYSTEM_LIB64 = "/system/lib64"
    _DUMP_DIR = os.path.join("vts", "testcases", "vndk", "golden")

    def setUpClass(self):
        """Initializes data file path, device, and temporary directory."""
        required_params = [keys.ConfigKeys.IKEY_DATA_FILE_PATH]
        self.getUserParams(required_params)

        self._dut = self.registerController(android_device)[0]
        self._temp_dir = tempfile.mkdtemp()
        self._vendor_lib = os.path.join(self._temp_dir, "vendor_lib")
        self._vendor_lib64 = os.path.join(self._temp_dir, "vendor_lib64")
        self._system_lib = os.path.join(self._temp_dir, "system_lib")
        self._system_lib64 = os.path.join(self._temp_dir, "system_lib64")
        logging.info("host lib dir: %s %s %s %s",
                     self._vendor_lib, self._vendor_lib64,
                     self._system_lib, self._system_lib64)
        self._PullOrCreateDir(self._TARGET_VENDOR_LIB, self._vendor_lib)
        self._PullOrCreateDir(self._TARGET_SYSTEM_LIB, self._system_lib)
        if self._dut.is64Bit:
            self._PullOrCreateDir(self._TARGET_VENDOR_LIB64, self._vendor_lib64)
            self._PullOrCreateDir(self._TARGET_SYSTEM_LIB64, self._system_lib64)

        cmd = "getprop ro.build.version.sdk"
        logging.info("adb shell %s", cmd)
        self._sdk_version = self._dut.adb.shell(cmd).rstrip()
        logging.info("sdk version: %s", self._sdk_version)

    def tearDownClass(self):
        """Deletes the temporary directory."""
        logging.info("Delete %s", self._temp_dir)
        shutil.rmtree(self._temp_dir)

    def _PullOrCreateDir(self, target_dir, host_dir):
        """Copies a directory from device. Creates an empty one if not exist.

        Args:
            target_dir: The directory to copy from device.
            host_dir: The directory to copy to host.
        """
        test_cmd = "test -d " + target_dir
        logging.info("adb shell %s", test_cmd)
        result = self._dut.adb.shell(test_cmd, no_except=True)
        if result[const.EXIT_CODE]:
            logging.info("%s doesn't exist. Create %s.", target_dir, host_dir)
            os.mkdir(host_dir, 0750)
            return
        logging.info("adb pull %s %s", target_dir, host_dir)
        pull_output = self._dut.adb.pull(target_dir, host_dir)
        logging.debug(pull_output)

    def _DiffSymbols(self, dump_path, lib_path):
        """Checks if a library includes all symbols in a dump.

        Args:
            dump_path: The path to the dump file containing list of symbols.
            lib_path: The path to the library.

        Returns:
            A list of strings, the global symbols that are in the dump but not
            in the library.

        Raises:
            IOError if fails to load the dump.
            elf_parser.ElfError if fails to load the library.
        """
        with open(dump_path, "r") as dump_file:
            dump_symbols = set(line.strip() for line in dump_file
                               if line.strip())
        parser = elf_parser.ElfParser(lib_path)
        try:
            lib_symbols = parser.ListGlobalDynamicSymbols()
        finally:
            parser.Close()
        logging.debug("%s: %s", lib_path, lib_symbols)
        return sorted(dump_symbols.difference(lib_symbols))

    def _DiffVtables(self, dump_path, lib_path):
        """Checks if a library includes all vtable entries in a dump.

        Args:
            dump_path: The path to the dump file containing vtables.
            lib_path: The path to the library.

        Returns:
            A list of tuples (VTABLE, SYMBOL, EXPECTED_OFFSET, ACTUAL_OFFSET).
            ACTUAL_OFFSET can be "missing" or numbers separated by comma.

        Raises:
            IOError if fails to load the dump.
            vtable_parser.VtableError if fails to load the library.
        """
        parser = vtable_parser.VtableParser(
                os.path.join(self.data_file_path, "host"))
        with open(dump_path, "r") as dump_file:
            dump_vtables = parser.ParseVtablesFromString(dump_file.read())

        lib_vtables = parser.ParseVtablesFromLibrary(lib_path)
        logging.debug("%s: %s", lib_path, lib_vtables)
        diff = []
        for vtable, dump_symbols in dump_vtables.iteritems():
            lib_inv_vtable = dict()
            if vtable in lib_vtables:
                for off, sym in lib_vtables[vtable]:
                    if sym not in lib_inv_vtable:
                        lib_inv_vtable[sym] = [off]
                    else:
                        lib_inv_vtable[sym].append(off)
            for off, sym in dump_symbols:
                if sym not in lib_inv_vtable:
                    diff.append((vtable, sym, str(off), "missing"))
                elif off not in lib_inv_vtable[sym]:
                    diff.append((vtable, sym, str(off),
                                 ",".join(str(x) for x in lib_inv_vtable[sym])))
        return diff

    def _ScanLibDirs(self, dump_dir, lib_dirs):
        """Compares dump files with libraries copied from device.

        Args:
            dump_dir: The directory containing dump files.
            lib_dirs: The list of directories containing libraries.

        Returns:
            An integer, number of incompatible libraries.
        """
        error_count = 0
        symbol_dumps = dict()
        vtable_dumps = dict()
        lib_paths = dict()
        for root_dir, file_name in utils.iterate_files(dump_dir):
            dump_path = os.path.join(root_dir, file_name)
            if file_name.endswith("_symbol.dump"):
                lib_name = file_name.rpartition("_symbol.dump")[0]
                symbol_dumps[lib_name] = dump_path
            elif file_name.endswith("_vtable.dump"):
                lib_name = file_name.rpartition("_vtable.dump")[0]
                vtable_dumps[lib_name] = dump_path
            else:
                logging.warning("Unknown dump: " + dump_path)
                continue
            lib_paths[lib_name] = None

        for lib_dir in lib_dirs:
            for root_dir, lib_name in utils.iterate_files(lib_dir):
                if lib_name in lib_paths and not lib_paths[lib_name]:
                    lib_paths[lib_name] = os.path.join(root_dir, lib_name)

        for lib_name, lib_path in lib_paths.iteritems():
            if not lib_path:
                logging.info("%s: Not found on target", lib_name)
                continue
            rel_path = os.path.relpath(lib_path, self._temp_dir)

            has_exception = False
            missing_symbols = []
            vtable_diff = []
            # Compare symbols
            if lib_name in symbol_dumps:
                try:
                    missing_symbols = self._DiffSymbols(
                            symbol_dumps[lib_name], lib_path)
                except (IOError, elf_parser.ElfError):
                    logging.exception("%s: Cannot diff symbols", rel_path)
                    has_exception = True
            # Compare vtables
            if lib_name in vtable_dumps:
                try:
                    vtable_diff = self._DiffVtables(
                            vtable_dumps[lib_name], lib_path)
                except (IOError, vtable_parser.VtableError):
                    logging.exception("%s: Cannot diff vtables", rel_path)
                    has_exception = True

            if missing_symbols:
                logging.error("%s: Missing Symbols:\n%s",
                              rel_path, "\n".join(missing_symbols))
            if vtable_diff:
                logging.error("%s: Vtable Difference:\n"
                              "vtable symbol expected actual\n%s",
                              rel_path,
                              "\n".join(" ".join(x) for x in vtable_diff))
            if  has_exception or missing_symbols or vtable_diff:
                error_count += 1
            else:
                logging.info("%s: Pass", rel_path)
        return error_count

    def testAbiCompatibility(self):
        """Checks ABI compliance of vendor-available libraries."""
        abi = self._dut.cpu_abi
        if abi.startswith("arm"):
            abi_32, abi_64 = "arm", "arm64"
        elif abi.startswith("x86"):
            abi_32, abi_64 = "x86", "x86_64"
        elif abi.startswith("mips"):
            abi_32, abi_64 = "mips", "mips64"
        else:
            asserts.fail("Unknown ABI " + abi)
        dump_dir_32 = os.path.join(self._DUMP_DIR, self._sdk_version, abi_32)
        dump_dir_64 = os.path.join(self._DUMP_DIR, self._sdk_version, abi_64)

        logging.info("Check 32-bit libraries")
        asserts.assertTrue(os.path.isdir(dump_dir_32),
                "No dump files for SDK version " + self._sdk_version)
        error_count = self._ScanLibDirs(dump_dir_32, [self._vendor_lib,
                                                      self._system_lib])
        if self._dut.is64Bit:
            logging.info("Check 64-bit libraries")
            asserts.assertTrue(os.path.isdir(dump_dir_64),
                    "No dump files for SDK version " + self._sdk_version)
            error_count += self._ScanLibDirs(dump_dir_64, [self._vendor_lib64,
                                                           self._system_lib64])
        asserts.assertEqual(error_count, 0,
                "Total number of errors: " + str(error_count))


if __name__ == "__main__":
    test_runner.main()
