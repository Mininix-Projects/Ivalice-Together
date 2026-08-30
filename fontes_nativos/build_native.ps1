param(
    [string]$OutputDirectory = (Join-Path $PSScriptRoot "build"),
    [switch]$PublishToSource
)

$ErrorActionPreference = "Stop"

function Assert-ToolIdentity {
    param(
        [System.Management.Automation.ApplicationInfo]$Tool,
        [string]$ExpectedPattern,
        [string]$Label
    )

    $versionText = (& $Tool.Source --version 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0 -or $versionText -notmatch $ExpectedPattern) {
        throw "$Label is not the required GNU tool: $($Tool.Source)"
    }
}

function Assert-CmdLiteralPath {
    param(
        [string]$Path,
        [string]$Label
    )

    if (-not $Path -or $Path -match '[%"\r\n]') {
        throw "$Label cannot be represented safely in the compiler command."
    }
}

function Count-BytePattern {
    param(
        [byte[]]$Data,
        [byte[]]$Pattern
    )

    if (-not $Pattern.Length -or $Data.Length -lt $Pattern.Length) {
        return 0
    }
    $count = 0
    for ($offset = 0; $offset -le $Data.Length - $Pattern.Length; $offset++) {
        $matches = $true
        for ($index = 0; $index -lt $Pattern.Length; $index++) {
            if ($Data[$offset + $index] -ne $Pattern[$index]) {
                $matches = $false
                break
            }
        }
        if ($matches) {
            $count++
        }
    }
    return $count
}

function Move-StagedArtifact {
    param(
        [string]$StageDirectory,
        [string]$DestinationDirectory,
        [string]$Name
    )

    $source = Join-Path $StageDirectory $Name
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Native build did not create $Name."
    }
    Move-Item -LiteralPath $source `
        -Destination (Join-Path $DestinationDirectory $Name) -Force
}

$vswhere = Join-Path ${env:ProgramFiles(x86)} `
    "Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path -LiteralPath $vswhere)) {
    throw "Visual Studio Installer was not found."
}

$visualStudio = & $vswhere -latest -products * `
    -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
    -property installationPath
if (-not $visualStudio) {
    throw "Visual Studio x64 C++ build tools were not found."
}

$assembler = Get-Command as.exe -CommandType Application -ErrorAction Stop
$objcopy = Get-Command objcopy.exe -CommandType Application -ErrorAction Stop
Assert-ToolIdentity $assembler "GNU assembler" "Assembler"
Assert-ToolIdentity $objcopy "GNU objcopy" "Object copier"
$vsdev = Join-Path $visualStudio "Common7\Tools\VsDevCmd.bat"

New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$OutputDirectory = (Resolve-Path -LiteralPath $OutputDirectory).Path
$buildToken = [Guid]::NewGuid().ToString("N")
$stageDirectory = Join-Path $OutputDirectory ".native-stage-$buildToken"
New-Item -ItemType Directory -Path $stageDirectory | Out-Null
$stageDirectory = (Resolve-Path -LiteralPath $stageDirectory).Path
$expectedStagePrefix = $OutputDirectory.TrimEnd('\') + '\'
if (-not $stageDirectory.StartsWith(
        $expectedStagePrefix,
        [StringComparison]::OrdinalIgnoreCase
    )) {
    throw "Native staging directory escaped the requested output directory."
}

$rendererSource = Join-Path $PSScriptRoot "p2_menu_color_dx11.cpp"
$autostartSource = Join-Path $PSScriptRoot "ivalice_together_autostart.cpp"
$cameraSource = Join-Path $PSScriptRoot "native_camera_collision_filter_v2.S"
$rendererName = "p2_menu_color_dx11_v85.dll"
$autostartName = "ivalice-together-autostart.dll"
$cameraName = "native_camera_collision_filter_v2.bin"
Assert-CmdLiteralPath $vsdev "Visual Studio environment path"
Assert-CmdLiteralPath $stageDirectory "Native staging path"
Assert-CmdLiteralPath $rendererSource "Renderer source path"
Assert-CmdLiteralPath $autostartSource "Autostart source path"

try {
    $rendererDll = Join-Path $stageDirectory $rendererName
    $rendererObject = Join-Path $stageDirectory "p2_menu_color_dx11_v85.obj"
    $compile = 'call "' + $vsdev + '" -arch=x64 -host_arch=x64 >nul && ' +
        'cl.exe /nologo /std:c++17 /O2 /EHsc /MT /LD /DNOMINMAX /W4 ' +
        '/Fo"' + $rendererObject + '" /Fe:"' + $rendererDll + '" ' +
        '"' + $rendererSource + '" /link /NOLOGO d3d11.lib dxgi.lib ' +
        'd3dcompiler.lib user32.lib gdi32.lib /Brepro'
    & cmd.exe /d /s /c $compile
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to compile the renderer."
    }

    $autostartDll = Join-Path $stageDirectory $autostartName
    $autostartObject = Join-Path $stageDirectory "ivalice-together-autostart.obj"
    $compileAutostart = 'call "' + $vsdev + '" -arch=x64 -host_arch=x64 >nul && ' +
        'cl.exe /nologo /std:c++17 /O2 /EHsc /MT /LD /DNOMINMAX /W4 ' +
        '/Fo"' + $autostartObject + '" /Fe:"' + $autostartDll + '" ' +
        '"' + $autostartSource + '" /link /NOLOGO user32.lib /Brepro'
    & cmd.exe /d /s /c $compileAutostart
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to compile the module-loader autostart component."
    }

    $cameraObject = Join-Path $stageDirectory "native_camera_collision_filter_v2.o"
    $cameraBinary = Join-Path $stageDirectory $cameraName
    & $assembler.Source --64 -o $cameraObject $cameraSource
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to assemble the camera filter."
    }
    & $objcopy.Source -O binary -j .text $cameraObject $cameraBinary
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to generate the camera-filter binary."
    }

    $cameraBytes = [IO.File]::ReadAllBytes($cameraBinary)
    if ($cameraBytes.Length -le 0 -or $cameraBytes.Length -ge 0x3E0) {
        throw "Camera-filter binary escaped its reserved cave span."
    }
    $markerContracts = @(
        [PSCustomObject]@{
            Pattern = [BitConverter]::GetBytes([UInt64]0x1111111122222222)
            Expected = 1
        },
        [PSCustomObject]@{
            Pattern = [BitConverter]::GetBytes([UInt64]0x3333333344444444)
            Expected = 4
        },
        [PSCustomObject]@{
            Pattern = [BitConverter]::GetBytes([UInt64]0x5555555566666666)
            Expected = 2
        }
    )
    foreach ($contract in $markerContracts) {
        $actual = Count-BytePattern $cameraBytes $contract.Pattern
        if ($actual -ne $contract.Expected) {
            throw "Camera-filter marker count drifted: expected $($contract.Expected), got $actual."
        }
    }

    foreach ($name in @(
            $rendererName,
            "p2_menu_color_dx11_v85.obj",
            "p2_menu_color_dx11_v85.lib",
            "p2_menu_color_dx11_v85.exp",
            $autostartName,
            "ivalice-together-autostart.obj",
            "ivalice-together-autostart.lib",
            "ivalice-together-autostart.exp",
            "native_camera_collision_filter_v2.o",
            $cameraName
        )) {
        Move-StagedArtifact $stageDirectory $OutputDirectory $name
    }

    $sourceHashes = [ordered]@{
        "p2_menu_color_dx11.cpp" = (Get-FileHash -Algorithm SHA256 $rendererSource).Hash
        "ivalice_together_autostart.cpp" = (Get-FileHash -Algorithm SHA256 $autostartSource).Hash
        "native_camera_collision_filter_v2.S" = (Get-FileHash -Algorithm SHA256 $cameraSource).Hash
    }
    $artifactHashes = [ordered]@{}
    foreach ($name in @($rendererName, $autostartName, $cameraName)) {
        $artifactHashes[$name] = (
            Get-FileHash -Algorithm SHA256 (Join-Path $OutputDirectory $name)
        ).Hash
    }
    $manifest = [ordered]@{
        schema = 1
        sources = $sourceHashes
        artifacts = $artifactHashes
    } | ConvertTo-Json -Depth 4

    if ($PublishToSource) {
        $sourceDirectory = (Resolve-Path `
            -LiteralPath (Join-Path $PSScriptRoot "..\source")).Path
        foreach ($name in @($rendererName, $autostartName, $cameraName)) {
            $temporary = Join-Path $sourceDirectory ".$name.$buildToken.tmp"
            Copy-Item -LiteralPath (Join-Path $OutputDirectory $name) `
                -Destination $temporary
            Move-Item -LiteralPath $temporary `
                -Destination (Join-Path $sourceDirectory $name) -Force
        }
    }

    $manifestTemporary = Join-Path $OutputDirectory `
        ".native_build_manifest.$buildToken.tmp"
    [IO.File]::WriteAllText(
        $manifestTemporary,
        $manifest + [Environment]::NewLine,
        [Text.UTF8Encoding]::new($false)
    )
    Move-Item -LiteralPath $manifestTemporary `
        -Destination (Join-Path $OutputDirectory "native_build_manifest.json") `
        -Force

    Get-FileHash -Algorithm SHA256 `
        (Join-Path $OutputDirectory $rendererName), `
        (Join-Path $OutputDirectory $autostartName), `
        (Join-Path $OutputDirectory $cameraName) |
        Select-Object Path, Hash
} finally {
    if (Test-Path -LiteralPath $stageDirectory) {
        $resolvedStage = (Resolve-Path -LiteralPath $stageDirectory).Path
        if (-not $resolvedStage.StartsWith(
                $expectedStagePrefix,
                [StringComparison]::OrdinalIgnoreCase
            )) {
            throw "Refusing to clean a staging directory outside native output."
        }
        Remove-Item -LiteralPath $resolvedStage -Recurse -Force
    }
}
