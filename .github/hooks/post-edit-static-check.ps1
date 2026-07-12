Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$editToolNames = @(
    'apply_patch',
    'create_file',
    'edit_notebook_file',
    'vscode_renameSymbol',
    'mcp_pylance_mcp_s_pylanceInvokeRefactoring'
)

$validationRelevantExtensions = @(
    '.py',
    '.json',
    '.yml',
    '.yaml',
    '.toml',
    '.ps1',
    '.psm1',
    '.bat',
    '.cmd',
    '.ipynb'
)

function Emit-Continue {
    param([string]$Message = '')

    $result = @{ 'continue' = $true }
    if ($Message) {
        $result['systemMessage'] = $Message
    }

    $result | ConvertTo-Json -Depth 5 -Compress
}

function Emit-Block {
    param([string[]]$Files)

    $joined = $Files -join ', '
    $message = "检测到编辑工具已触达文件: $joined。下一步请先对这些文件运行聚焦静态检查，再继续修改或结束任务。"
    $additionalContext = "You just edited these files: $joined. Before more edits or completion, run focused static validation on the touched files. At minimum, run diagnostics/errors for each touched Python or config file. If Python imports, module boundaries, or flow_configs.json changed, also run a focused import or structure validation."

    @{
        'continue' = $true
        'systemMessage' = $message
        'decision' = 'block'
        'reason' = 'Edited files require focused static validation before continuing.'
        'hookSpecificOutput' = @{
            'hookEventName' = 'PostToolUse'
            'additionalContext' = $additionalContext
        }
    } | ConvertTo-Json -Depth 6 -Compress
}

function Resolve-InputPath {
    param(
        [string]$Value,
        [string]$Cwd
    )

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $null
    }

    $normalized = $Value.Trim()
    if ($normalized.StartsWith('file://', [System.StringComparison]::OrdinalIgnoreCase)) {
        try {
            $normalized = ([System.Uri]$normalized).LocalPath
        }
        catch {
            return $null
        }
    }

    if (-not [System.IO.Path]::IsPathRooted($normalized) -and -not [string]::IsNullOrWhiteSpace($Cwd)) {
        try {
            $normalized = [System.IO.Path]::GetFullPath((Join-Path $Cwd $normalized))
        }
        catch {
            return $null
        }
    }

    return $normalized
}

try {
    $rawInput = [Console]::In.ReadToEnd()
    if ([string]::IsNullOrWhiteSpace($rawInput)) {
        Emit-Continue
        exit 0
    }

    $hookInput = $rawInput | ConvertFrom-Json -Depth 100
    $toolName = [string]$hookInput.tool_name
    if (-not ($editToolNames -contains $toolName)) {
        Emit-Continue
        exit 0
    }

    $cwd = [string]$hookInput.cwd
    $toolInput = $hookInput.tool_input
    $touchedFiles = New-Object System.Collections.ArrayList

    if ($toolName -eq 'apply_patch') {
        $patchText = [string]$toolInput.input
        if (-not [string]::IsNullOrWhiteSpace($patchText)) {
            $matches = [regex]::Matches($patchText, '(?m)^\*\*\* (?:Update|Add|Delete) File: (.+?)(?:\s*->\s*.+)?$')
            foreach ($match in $matches) {
                if ($match.Groups.Count -lt 2) {
                    continue
                }
                $resolved = Resolve-InputPath -Value $match.Groups[1].Value.Trim() -Cwd $cwd
                if ($resolved -and -not ($touchedFiles -contains $resolved)) {
                    [void]$touchedFiles.Add($resolved)
                }
            }
        }
    }
    else {
        foreach ($propertyName in @('filePath', 'path', 'fileUri', 'uri')) {
            if ($toolInput.PSObject.Properties.Name -contains $propertyName) {
                $resolved = Resolve-InputPath -Value ([string]$toolInput.$propertyName) -Cwd $cwd
                if ($resolved -and -not ($touchedFiles -contains $resolved)) {
                    [void]$touchedFiles.Add($resolved)
                }
            }
        }

        foreach ($propertyName in @('filePaths', 'paths')) {
            if ($toolInput.PSObject.Properties.Name -contains $propertyName) {
                foreach ($value in $toolInput.$propertyName) {
                    $resolved = Resolve-InputPath -Value ([string]$value) -Cwd $cwd
                    if ($resolved -and -not ($touchedFiles -contains $resolved)) {
                        [void]$touchedFiles.Add($resolved)
                    }
                }
            }
        }
    }

    $files = @($touchedFiles)
    if (-not $files.Count) {
        Emit-Continue
        exit 0
    }

    $needsValidation = $false
    foreach ($file in $files) {
        $fileName = [System.IO.Path]::GetFileName($file)
        $extension = [System.IO.Path]::GetExtension($file)
        if ($fileName -ieq 'flow_configs.json') {
            $needsValidation = $true
            break
        }
        if ($extension -and ($validationRelevantExtensions -contains $extension.ToLowerInvariant())) {
            $needsValidation = $true
            break
        }
    }

    if (-not $needsValidation) {
        Emit-Continue
        exit 0
    }

    Emit-Block -Files $files
    exit 0
}
catch {
    Emit-Continue -Message ('post-edit-static-check hook failed: ' + $_.Exception.Message)
    exit 0
}