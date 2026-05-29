# run_queries.ps1 - Session 7 regression suite.
# Runs the 8 base queries (10 invocations) one at a time, pausing between
# each so you can inspect iteration counts and the FINAL answer.
#
# Usage:
#   .\run_queries.ps1            # run all, pause between each
#   .\run_queries.ps1 -NoPause   # run all back-to-back
#   .\run_queries.ps1 -Only A,E  # run only the named queries
#   .\run_queries.ps1 -Fresh     # wipe state/ before starting
#
# Ordering matters: C2 must follow C1, F2 must follow F1 (cross-run memory).

param(
    [string[]]$Only,
    [switch]$NoPause,
    [switch]$Fresh
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# label, expected-iters note, query
$queries = @(
    @{ id = "A";  note = "Shannon Wikipedia (~3 iters)";                  q = "Fetch the Wikipedia page for Claude Shannon and tell me what he is most famous for." },
    @{ id = "B";  note = "Tokyo activities + Saturday weather (~8 iters)"; q = "Suggest 3 activities to do in Tokyo on Saturday based on the weather forecast." },
    @{ id = "C1"; note = "Mom's birthday SAVE (~4 iters)";                q = "My mom's birthday is on 15 May 2026. Save a reminder note and another reminder two weeks before." },
    @{ id = "C2"; note = "Mom's birthday RECALL (~3 iters, ZERO tools)";  q = "When is my mom's birthday?" },
    @{ id = "D";  note = "Asyncio research (~6 iters)";                    q = "Research Python asyncio best practices from the top 3 web results and summarise them." },
    @{ id = "E";  note = "Index one paper (~5 iters)";                    q = "Index the file papers/attention.md and then tell me what attention mechanism it describes." },
    @{ id = "F1"; note = "Index every .md, then ask (~11 iters)";         q = "Index every .md file under papers/ and then tell me which paper introduces LoRA." },
    @{ id = "F2"; note = "Cross-process retrieval (~3 iters)";            q = "Across the indexed papers, which one is about Direct Preference Optimization?" },
    @{ id = "G";  note = "Dense retrieval / credit assignment (~4 iters)"; q = "What do the indexed papers say about the credit assignment problem?" },
    @{ id = "H";  note = "ReAct vs CoT synthesis (~3 iters)";             q = "Compare ReAct and Chain-of-Thought based on the indexed papers and cite which paper each idea came from." }
)

if ($Fresh) {
    Write-Host "[fresh] wiping state/" -ForegroundColor Yellow
    Remove-Item state\memory.json, state\index.faiss, state\index_ids.json -ErrorAction SilentlyContinue
    Remove-Item state\artifacts\* -ErrorAction SilentlyContinue
}

foreach ($item in $queries) {
    if ($Only -and ($Only -notcontains $item.id)) { continue }

    Write-Host ""
    Write-Host ("#" * 78) -ForegroundColor Cyan
    Write-Host ("# Query {0} - {1}" -f $item.id, $item.note) -ForegroundColor Cyan
    Write-Host ("#" * 78) -ForegroundColor Cyan

    uv run agent7.py $item.q

    if (-not $NoPause -and $item -ne $queries[-1]) {
        Read-Host "`n--- Query $($item.id) done. Press Enter for the next query (Ctrl+C to stop) ---"
    }
}

Write-Host "`nAll requested queries complete." -ForegroundColor Green
