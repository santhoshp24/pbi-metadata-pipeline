<#
.SYNOPSIS
  Stage 1 - Export semantic model (TMDL) and report (PBIR) definitions from Microsoft Fabric / Power BI
  workspaces using the Fabric REST "Get Item Definition" API.

.DESCRIPTION
  Authenticates as the signed-in USER (delegated auth, no service principal needed), so you only
  see what your own account can already access. Handles the long-running-operation (202 + Location
  header) pattern and decodes the base64 definition parts to disk.

.EXAMPLE
  ./Export-FabricDefinitions.ps1 -WorkspaceNames "Maritime-Analytics","Maritime-Finance" -OutDir ../output/raw

.NOTES
  Requires: Az.Accounts module (Install-Module Az.Accounts)
  Workspaces must be on Premium / Fabric capacity for Get Item Definition to work.
#>
[CmdletBinding()]
param(
  [string[]]$WorkspaceNames,                 # omit to export every workspace you can see
  [string]$OutDir = "./output/raw",
  [string[]]$ItemTypes = @("SemanticModel", "Report"),
  [int]$PollSeconds = 3,
  [int]$MaxPolls = 60
)

$ErrorActionPreference = "Stop"
$FabricApi = "https://api.fabric.microsoft.com/v1"

# .NET file APIs resolve relative paths against the process directory (often C:\Windows\System32),
# not the PowerShell location, so convert OutDir to an absolute path up front.
$OutDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutDir)

function Get-FabricHeaders {
  $token = (Get-AzAccessToken -ResourceUrl "https://api.fabric.microsoft.com").Token
  if ($token -is [System.Security.SecureString]) {
    $token = [System.Net.NetworkCredential]::new("", $token).Password
  }
  return @{ Authorization = "Bearer $token"; "Content-Type" = "application/json" }
}

# Follow continuationToken pagination for list endpoints
function Invoke-FabricList([string]$Url) {
  $results = @()
  do {
    $resp = Invoke-RestMethod -Uri $Url -Headers (Get-FabricHeaders) -Method Get
    $results += $resp.value
    $Url = if ($resp.continuationUri) { $resp.continuationUri } else { $null }
  } while ($Url)
  return $results
}

# POST getDefinition, then poll the long-running operation until it finishes
function Get-ItemDefinition([string]$WorkspaceId, [string]$ItemId, [string]$Format) {
  $url = "$FabricApi/workspaces/$WorkspaceId/items/$ItemId/getDefinition"
  if ($Format) { $url += "?format=$Format" }

  $resp = Invoke-WebRequest -UseBasicParsing -Uri $url -Headers (Get-FabricHeaders) -Method Post -Body "{}"
  if ($resp.StatusCode -eq 200) { return ($resp.Content | ConvertFrom-Json).definition }

  # 202 Accepted -> poll Location, then fetch /result
  $location = $resp.Headers["Location"]; if ($location -is [array]) { $location = $location[0] }
  for ($i = 0; $i -lt $MaxPolls; $i++) {
    Start-Sleep -Seconds $PollSeconds
    $op = Invoke-RestMethod -Uri $location -Headers (Get-FabricHeaders) -Method Get
    if ($op.status -eq "Succeeded") {
      $result = Invoke-RestMethod -Uri "$location/result" -Headers (Get-FabricHeaders) -Method Get
      return $result.definition
    }
    if ($op.status -eq "Failed") { throw "getDefinition failed: $($op.error.message)" }
  }
  throw "Timed out waiting for getDefinition on item $ItemId"
}

function Save-Definition($Definition, [string]$TargetDir) {
  foreach ($part in $Definition.parts) {
    $path = Join-Path $TargetDir $part.path
    New-Item -ItemType Directory -Force -Path (Split-Path $path) | Out-Null
    [IO.File]::WriteAllBytes($path, [Convert]::FromBase64String($part.payload))
  }
}

# --- main -------------------------------------------------------------------
if (-not (Get-AzContext)) { Connect-AzAccount | Out-Null }   # interactive user login

$workspaces = Invoke-FabricList "$FabricApi/workspaces"
if ($WorkspaceNames) { $workspaces = $workspaces | Where-Object { $WorkspaceNames -contains $_.displayName } }
Write-Host "Exporting from $($workspaces.Count) workspace(s)..."

$manifest = @()
foreach ($ws in $workspaces) {
  $safeWs = ($ws.displayName -replace '[\\/:*?"<>|]', "_")
  $items = Invoke-FabricList "$FabricApi/workspaces/$($ws.id)/items" |
           Where-Object { $ItemTypes -contains $_.type }

  foreach ($item in $items) {
    $safeName = ($item.displayName -replace '[\\/:*?"<>|]', "_")
    $target = Join-Path $OutDir "$safeWs/$safeName.$($item.type)"
    try {
      # TMDL for models; PBIR (default JSON) for reports
      $format = if ($item.type -eq "SemanticModel") { "TMDL" } else { $null }
      $def = Get-ItemDefinition -WorkspaceId $ws.id -ItemId $item.id -Format $format
      Save-Definition -Definition $def -TargetDir $target
      $status = "ok"
    }
    catch {
      Write-Warning "[$safeWs] $($item.displayName): $($_.Exception.Message)"
      $status = "failed: $($_.Exception.Message)"
    }
    $manifest += [pscustomobject]@{
      workspace = $ws.displayName; item = $item.displayName; type = $item.type
      itemId = $item.id; workspaceId = $ws.id; status = $status
    }
    Start-Sleep -Milliseconds 500   # be polite to the API (throttling)
  }
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$manifest | ConvertTo-Json -Depth 3 | Set-Content (Join-Path $OutDir "_manifest.json")
Write-Host "Done. $($manifest.Count) items processed. Manifest: $OutDir/_manifest.json"
