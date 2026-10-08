# Desktop bundle notice checks

Prepare notices before building the installer payload. `build_installer.py`
checks file integrity and known private paths; it does not determine whether
third-party license obligations have been satisfied.

1. Preserve the license supplied with the exact CPython runtime archive. Include
   the project license from the reviewed release source and the incorporated
   software licenses from documentation matching the bundled Python version.
2. Preserve Tcl/Tk license terms and notices included in individual runtime data
   files. The embeddable core and the frozen GUI use separate Python layouts;
   inspect both, including native libraries linked into extension modules.
3. Review the installed PyInstaller `COPYING.txt`, loader and collected runtime
   hook headers. Keep their notice texts with the candidate. Build dependencies
   such as packaging tools must not be assumed to be bundled runtime modules.
4. Match additional native components to their source notices. For the current
   Python 3.14.6 candidate these include zlib-ng, XZ/liblzma and SQLite. Record
   which versions were read from runtime APIs and which were inferred from the
   exact CPython build inputs; do not label an inferred version as measured.
   Also inspect licenses in incorporated source headers: the CPython 3.14.6
   HACL* directory contains multiple notice variants that are absent from the
   general Python license page. Preserve each applicable variant and its terms.
5. Store source URLs, source artifact versions and SHA-256 values in the build
   evidence. Keep private build-machine paths outside the distributed inventory.
   Bind supplier SPDX metadata to the actual release archive checksum. An empty
   upstream files list does not provide file-level attribution, and a broad
   supplier package list is not the application's own dependency inventory.
6. Inspect both frozen executables' embedded archives and their Analysis TOCs.
   Verify that the installer's embedded payload matches the reviewed payload,
   and that the installed notice files match the candidate byte for byte.
7. Place an identical `notices` directory beside the installer in the retained
   distribution so the terms are available before installation as well as inside
   the installed version. Package removal must preserve installed notices.

Primary references:

- [Python license and incorporated software notices](https://docs.python.org/3.14/license.html).
- [Exact CPython 3.14.6 Windows dependency inputs](https://raw.githubusercontent.com/python/cpython/v3.14.6/PCbuild/python.props).
- [PyInstaller licensing and dependency distinction](https://pyinstaller.org/en/stable/license.html).
- [zlib-ng 2.2.4 notice](https://raw.githubusercontent.com/zlib-ng/zlib-ng/2.2.4/LICENSE.md).
- [XZ 5.2.5 component licensing](https://raw.githubusercontent.com/tukaani-project/xz/v5.2.5/COPYING).
- [SQLite copyright information](https://www.sqlite.org/copyright.html).

Completing this inventory does not complete Microsoft runtime redistribution
review, signing decisions, platform testing, or public-release review. Keep those
results separate from successful artifact hashing or mock execution.

CPython's Windows binary build includes additional Microsoft runtime conditions.
Preserve that text and review the distributor's applicable rights and recipient
terms before publication. Merely including a notice is not evidence of recipient
agreement. Do not silently add a new license or interpret an existing Install
button as acceptance of newly introduced terms.
