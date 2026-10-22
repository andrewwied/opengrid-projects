[CmdletBinding()]
param(
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]]$BuildArguments
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$toolRoot = Join-Path $repoRoot '.tools'
$lockPath = Join-Path $repoRoot 'tools\tool-lock.json'
$lock = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json
$platform = 'windows-x86_64'
$pythonSpec = $lock.python.$platform

if ($null -eq $pythonSpec) {
  throw "No managed Python runtime is locked for $platform."
}

$runtimeDir = Join-Path $toolRoot "python\$($pythonSpec.version)\$platform"
$python = Join-Path $runtimeDir 'python.exe'

function Test-Sha256 {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$Expected
  )

  $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($actual -ne $Expected.ToLowerInvariant()) {
    throw "Checksum mismatch for $Path. Expected $Expected, received $actual."
  }
}

function Expand-CheckedArchive {
  param(
    [Parameter(Mandatory = $true)][string]$Archive,
    [Parameter(Mandatory = $true)][string]$Destination
  )

  Add-Type -AssemblyName System.IO.Compression.FileSystem
  $destinationFull = [System.IO.Path]::GetFullPath($Destination)
  $destinationPrefix = "$destinationFull$([System.IO.Path]::DirectorySeparatorChar)"
  $zip = [System.IO.Compression.ZipFile]::OpenRead($Archive)
  try {
    foreach ($entry in $zip.Entries) {
      $target = [System.IO.Path]::GetFullPath((Join-Path $destinationFull $entry.FullName))
      if ($target -ne $destinationFull -and -not $target.StartsWith($destinationPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe archive path in ${Archive}: $($entry.FullName)"
      }
      if ($entry.FullName.EndsWith('/')) {
        New-Item -ItemType Directory -Force -Path $target | Out-Null
      }
      elseif ($entry.Name) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
        [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
      }
    }
  }
  finally {
    $zip.Dispose()
  }
}
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
  $archive = Join-Path $toolRoot "downloads\python-$($pythonSpec.version)-$platform.zip"
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $archive) | Out-Null

  if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
    Write-Host "Downloading managed Python $($pythonSpec.version)..."
    Invoke-WebRequest -Uri $pythonSpec.url -OutFile $archive -UseBasicParsing
  }
  Test-Sha256 -Path $archive -Expected $pythonSpec.sha256

  $temporaryDir = "$runtimeDir.partial-$PID"
  Remove-Item -LiteralPath $temporaryDir -Recurse -Force -ErrorAction SilentlyContinue
  New-Item -ItemType Directory -Force -Path $temporaryDir | Out-Null
  Expand-CheckedArchive -Archive $archive -Destination $temporaryDir

  if (-not (Test-Path -LiteralPath (Join-Path $temporaryDir 'python.exe') -PathType Leaf)) {
    throw "The managed Python archive did not contain python.exe."
  }

  Remove-Item -LiteralPath $runtimeDir -Recurse -Force -ErrorAction SilentlyContinue
  Move-Item -LiteralPath $temporaryDir -Destination $runtimeDir
}

& $python (Join-Path $repoRoot 'tools\build.py') @BuildArguments
exit $LASTEXITCODE
