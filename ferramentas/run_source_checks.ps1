$ErrorActionPreference = "Stop"

$development = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$source = Join-Path $development "source"
$tests = Join-Path $development "testes"
$assetFreeTests = Join-Path $tests "asset_free_tests.txt"
$releaseBuilder = Join-Path $PSScriptRoot "build_public_release.py"
$releaseAudit = Join-Path $PSScriptRoot "audit_release_source.py"
$requiredFont = Join-Path $source `
    "extracted_ui_assets\gamedata\d3d11\artdata\font\us\font00.dat"
$publicToolSources = @(
    "add_party_member.py",
    "analyze_action_queue_trace.py",
    "audit_release_source.py",
    "build_public_release.py",
    "controller_config_gui.py",
    "headless_targeting.py",
    "interaction_transaction_model.py",
    "live_inventory.py",
    "menu_builder_snapshot_trace.py",
    "menu_native_snapshot.py",
    "native_damage_glyph_capture.py",
    "native_menu_catalog_provider.py",
    "native_menu_trace_merge.py",
    "native_queue_analysis.py",
    "native_render_object_create_capture.py",
    "native_sprite_draw_capture.py",
    "p2_native_catalog_pages.py",
    "tool_theme.py"
) | ForEach-Object { Join-Path $PSScriptRoot $_ }

$previousPythonPath = $env:PYTHONPATH
$previousCachePrefix = $env:PYTHONPYCACHEPREFIX
$env:PYTHONPATH = "$source;$PSScriptRoot"
$env:PYTHONPYCACHEPREFIX = Join-Path $development "artefatos\pycache"
Push-Location $development
try {
    python -m ruff check $source $tests @publicToolSources
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    python -m compileall -q $source $tests @publicToolSources
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    python $releaseAudit
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    if (Test-Path -LiteralPath $requiredFont -PathType Leaf) {
        python -m pytest -q $tests
    } else {
        Write-Host "Build-only game assets are absent; running asset-free tests."
        $testArguments = Get-Content -LiteralPath $assetFreeTests |
            Where-Object { $_ -and -not $_.StartsWith("#") } |
            ForEach-Object { Join-Path $tests $_ }
        python -m pytest -q @testArguments
    }
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
    $env:PYTHONPATH = $previousPythonPath
    $env:PYTHONPYCACHEPREFIX = $previousCachePrefix
}
