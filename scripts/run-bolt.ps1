<#
  NVIDIA-Nemotron-3.5-Bolt-E4B (UD-Q8_K_XL, unsloth)  --  dense, fully on GPU, vision + audio
  llama.cpp b12419 CUDA 13.5  /  RTX 5070 Ti Laptop (sm_120, 12 GB)

  arch gemma4 (NVIDIA post-train of Gemma 4 E4B-it), 7.5B params, 42 layers, 131072 ctx.
  Verified from GGUF metadata. Weights 7.63 GiB, but 2.84 GiB of that is the per-layer
  embedding table (E4B = "effective 4B"), which llama.cpp keeps in system RAM; only ~4.8 GiB
  goes to VRAM. KV is tiny for its context:
    - 18 of 42 layers reuse earlier layers' KV (shared_kv_layers), so only 24 own a cache
    - of those 24, 20 are sliding-window (512 tokens) and only 4 are global:
         2(K+V) x 2 kv_heads x 512 head_dim x 2 B = 4 KiB/layer -> 16 KiB/token @ f16
         32K = 0.5 GiB    64K = 1 GiB    128K = 2 GiB

  llama-fit-params estimate, MiB on CUDA0, ctx 128K, -ub 512:
    model 4942 + KV 2088 + compute 692 = 7722, + mmproj ~945 => 8667
    -ub 2048 costs 2826 MiB of compute (262K-token vocab), so this script keeps -ub 512.
    -fa auto resolves to on (same estimate); -fa off would need 2658 MiB of compute.
  MEASURED 2026-11-15 @128K, vision+audio on (nvidia-smi, load -> peak after a 51K prompt):
    -NoDFlash : 8522 -> 8732 MiB (71%)    DFlash : 9978 -> 10234 MiB (84%)
  Keep nvidia-smi at or below ~11250 MiB (92%) -- past that WDDM pages VRAM to RAM and speed
  collapses.

  Vision AND audio: mmproj-F16 holds both encoders (gemma4v + gemma4a). Send images as
  image_url parts and audio as input_audio parts through the OpenAI chat API. -NoVision skips
  the mmproj and frees ~0.9 GiB.

  DFlash (default ON): the repo ships a matching 5-layer, 360M block-diffusion drafter
  (block_size 8). MEASURED, 400-token outputs, tok/s without -> with (draft acceptance):
       prose 71-79 -> 121 (18%)    code 76 -> 171-174 (32%)    json 76 -> 317-354 (77%)
       51K-deep 58-62 -> 160-183 (50%)    prose temp 0.7 71-76 -> 103-108 (14%)
  Even 14-18% acceptance pays: each step drafts a whole 8-token block in one pass, and
  verifying 8 tokens costs a fully-GPU dense model about the same as 1.
  COST: prefill drops ~43% (51K prompt: ~6450 -> ~3650 t/s), +1.5 GiB VRAM, ~4 s more load.
  Use -NoDFlash when the workload is mostly long, fresh prompts with short answers.
  Tools, image and audio all verified working with DFlash on. Lessons from run-dflash.ps1
  (b10673): no explicit -ngl, -ub >= -b, n-max <= 7. On b12419 the --fit pass logs
  "failed to measure the memory of the extra model, fitting without it" -- expected, harmless.

  Sampling defaults come from the GGUF (general.sampling.*): temp 1.0 / top_k 64 / top_p 0.95.
  They're set as server defaults; a client that sends its own sampling params overrides them.

  Usage:
    .\run-bolt.ps1                        # ctx 131072, port 8080, vision+audio on, DFlash on
    .\run-bolt.ps1 -Reasoning off         # no thinking by default (NemoWispr turns it off per request anyway)
    .\run-bolt.ps1 -NoDFlash              # plain decoding: faster prefill, slower generation
    .\run-bolt.ps1 -Model NVIDIA-Nemotron-3.5-Bolt-E4B-UD-Q4_K_XL.gguf   # another Bolt quant in -ModelDir
    .\run-bolt.ps1 -Port 8081 -NoVision   # side-by-side test without the projector
    NemoWispr needs the projector (audio in) -- don't use -NoVision for it.
  Paths: -LlamaDir / -ModelDir, or $env:LLAMA_CPP_DIR / $env:BOLT_MODEL_DIR; defaults under %USERPROFILE%.
#>
param(
  [int]    $Ctx       = 131072,
  [int]    $Port      = 8080,
  [string] $Alias     = "bolt",
  [ValidateSet("auto", "on", "off")]
  [string] $Reasoning = "auto",    # thinking: template default, forced on, or off
  [switch] $NoVision,              # skip the vision/audio projector
  [switch] $NoDFlash,              # disable DFlash speculative decoding (on by default)
  [int]    $DraftMax  = 7,         # must be < dflash.block_size (8)
  [string] $Model     = "",        # Bolt GGUF file name in -ModelDir (or a full path); default UD-Q8_K_XL
  # llama.cpp CUDA build folder (llama-server.exe + its DLLs) and the unsloth GGUF snapshot folder
  [string] $LlamaDir  = $(if ($env:LLAMA_CPP_DIR) { $env:LLAMA_CPP_DIR } else { Join-Path $env:USERPROFILE "llama.cpp" }),
  [string] $ModelDir  = $(if ($env:BOLT_MODEL_DIR) { $env:BOLT_MODEL_DIR } else { Join-Path $env:USERPROFILE ".cache\huggingface\hub\models--unsloth--NVIDIA-Nemotron-3.5-Bolt-E4B-GGUF\snapshots\b738967d6da3441a7d1c71fcec15e49e32e16ecd" })
)

$ErrorActionPreference = "Stop"
$DFlash = -not $NoDFlash
$ROOT   = $LlamaDir
$DIR    = $ModelDir
$Q8     = "$DIR\NVIDIA-Nemotron-3.5-Bolt-E4B-UD-Q8_K_XL.gguf"
$ModelPath  = if (-not $Model) { $Q8 } elseif ([IO.Path]::IsPathRooted($Model)) { $Model } else { Join-Path $DIR $Model }
$MMPROJ = "$DIR\mmproj-F16.gguf"
$DRAFT  = "$DIR\dflash-NVIDIA-Nemotron-3.5-Bolt-E4B-Q8_0.gguf"

# exact sizes guard against a partial download (a new HF revision lands in a new snapshot dir)
$need = @{}
if ($ModelPath -eq $Q8) { $need[$ModelPath] = 8192955584 }        # other quants: must exist, with a sanity floor
elseif (-not (Test-Path $ModelPath)) { throw "missing: $ModelPath" }
elseif ((Get-Item $ModelPath).Length -lt 1GB) { throw "$(Split-Path $ModelPath -Leaf) is under 1 GB -- download incomplete?" }
if (-not $NoVision) { $need[$MMPROJ] = 990373568 }
if ($DFlash)        { $need[$DRAFT]  = 398892256 }
if (-not (Test-Path "$ROOT\llama-server.exe")) { throw "missing: $ROOT\llama-server.exe" }
foreach ($f in $need.Keys) {
  if (-not (Test-Path $f)) { throw "missing: $f" }
  $sz = (Get-Item $f).Length
  if ($sz -ne $need[$f]) { throw "$(Split-Path $f -Leaf) is $sz bytes, expected $($need[$f]) -- download incomplete?" }
}

# KV fit: 40 MiB of sliding-window cache + 16 KiB/token for the 4 global layers (matches fit-params).
# Peak = fit-params estimate + ~65 MiB measured; DFlash adds ~1500 MiB measured (draft + its buffers).
$kvMiB  = [math]::Round(40 + $Ctx * 16 / 1024)
# GPU weights scale with the file: 4942 MiB of the 8192955584-byte Q8_K_XL (the rest is the PLE table, in RAM).
$wMiB   = [math]::Round(4942 * (Get-Item $ModelPath).Length / 8192955584)
$estMiB = $wMiB + $kvMiB + 692 + 65 + $(if ($NoVision) { 0 } else { 945 }) + $(if ($DFlash) { 1500 } else { 0 })
Write-Host ("$(Split-Path $ModelPath -Leaf)  ctx=$Ctx  KV~{0:N1} GiB  vision+audio=$(-not $NoVision)  reasoning=$Reasoning  dflash=$DFlash  alias=$Alias  -> http://127.0.0.1:$Port" -f ($kvMiB / 1024)) -ForegroundColor Cyan
Write-Host ("  estimated VRAM ~{0:N0} MiB ({1:P0} of 12227)" -f $estMiB, ($estMiB / 12227)) -ForegroundColor DarkCyan
if ($estMiB -gt 11250) { Write-Warning "estimate is over the ~92% WDDM cliff -- lower -Ctx or add -NoVision" }

$a = @(
  "-m", $ModelPath,
  "--alias", $Alias,
  "-c", "$Ctx",
  "-np", "1",              # one slot; auto can allocate N x the KV cache
  "-fa", "on",
  "-b", "512", "-ub", "512",   # -ub 2048 = +2.1 GiB compute; -b = -ub also satisfies DFlash
  "--jinja",               # REQUIRED: Gemma 4 tool-call/thinking template
  "--reasoning", $Reasoning,
  "--temp", "1.0", "--top-k", "64", "--top-p", "0.95",
  "-t", "12",
  "--host", "127.0.0.1", "--port", "$Port"
)
if (-not $DFlash) { $a += @("-ngl", "all") }   # dense, fits entirely; with DFlash, let --fit place it
if (-not $NoVision) { $a += @("--mmproj", $MMPROJ) }
if ($DFlash) {
  $a += @("-md", $DRAFT, "--spec-type", "draft-dflash", "--spec-draft-n-max", "$DraftMax")
}

Push-Location $ROOT
try   { & "$ROOT\llama-server.exe" @a }
finally { Pop-Location }
