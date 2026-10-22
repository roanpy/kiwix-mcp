# Third-party notices

## OpenCC character mapping

`zh_variants.py` contains a Python character mapping derived from the OpenCC
source dictionary [`data/dictionary/TSCharacters.txt`](https://github.com/BYVoid/OpenCC/blob/master/data/dictionary/TSCharacters.txt).
OpenCC is a project by BYVoid and contributors, licensed under the Apache
License, Version 2.0. The full license text is included at
`LICENSES/Apache-2.0.txt`.

The table was modified when incorporated into this project: one-to-one entries
were represented as a Python dictionary, multi-output entries were omitted, and
the reverse simplified-to-traditional map excludes ambiguous collisions. The
exact upstream revision originally used for the import was not recorded and is
therefore not stated here.
