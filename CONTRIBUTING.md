# Contributing

Thank you for helping improve Ivalice Together.

## Bug reports

Include the following information:

- game version and Windows version;
- manual, MO2, or Vortex installation;
- controller model, connection method, and any emulation layer;
- exact reproduction steps;
- expected and observed behavior;
- a stable log excerpt when one is available.

Do not upload copyrighted game archives, save data containing private
information, or unrelated logs.

## Code changes

1. Keep production comments and identifiers in English.
2. Keep extracted game assets, captures, generated binaries, and personal
   configuration out of commits.
3. Add or update regression tests for behavior changes.
4. Run the complete source checks before opening a pull request:

```powershell
powershell -ExecutionPolicy Bypass -File ferramentas/run_source_checks.ps1
```

Changes to native hooks must validate original bytes, preserve rollback
behavior, and document any new executable-version assumption.
