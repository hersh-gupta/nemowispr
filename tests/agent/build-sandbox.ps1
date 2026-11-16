# Builds AgentSandbox.exe (a harmless WinForms window for the agent tests) from AgentSandbox.cs.
# Uses Windows PowerShell 5.1 (powershell.exe): its Add-Type compiles against .NET Framework and
# supports -OutputType WindowsApplication; PowerShell 7's Add-Type can't produce a WinForms exe.
# The agent refuses windows owned by powershell.exe, which is why the sandbox has to be its own exe.
$S = $PSScriptRoot
powershell.exe -NoProfile -Command "Add-Type -TypeDefinition (Get-Content '$S\AgentSandbox.cs' -Raw) -ReferencedAssemblies System.Windows.Forms, System.Drawing -OutputAssembly '$S\AgentSandbox.exe' -OutputType WindowsApplication"
if ($LASTEXITCODE) { throw "AgentSandbox.exe build failed" }
"built $S\AgentSandbox.exe"
