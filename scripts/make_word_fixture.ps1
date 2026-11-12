# Build (or open) a small Word document with real heading styles, for the
# reading acceptance. Word is driven through COM, so this only runs where Word
# is installed; the acceptance skips its Word section when it is not.
#
#   powershell -NoProfile -File scripts/make_word_fixture.ps1 -Path .probe\word_reading.docx
#   powershell -NoProfile -File scripts/make_word_fixture.ps1 -Path .probe\word_reading.docx -Open
#
# `-Open` leaves Word visible with the document open, which is how the reading
# acceptance gets a Word window without going through file association (that
# opens documents in Protected View, which has no browse mode).
param(
  [Parameter(Mandatory = $true)][string]$Path,
  [switch]$Open
)
$ErrorActionPreference = "Stop"
$full = [System.IO.Path]::GetFullPath($Path)
New-Item -ItemType Directory -Force -Path (Split-Path $full) | Out-Null

if ($Open) {
  if (-not (Test-Path $full)) { throw "no document at $full" }
  $word = New-Object -ComObject Word.Application
  $word.Visible = $true
  $null = $word.Documents.Open($full)
  Write-Output "opened $full"
  return
}

$word = New-Object -ComObject Word.Application
$word.Visible = $false
try {
  $doc = $word.Documents.Add()
  $selection = $word.Selection
  # Built-in style ids: -2 Heading 1, -3 Heading 2, -4 Heading 3, -1 Normal.
  $selection.Style = $doc.Styles.Item(-2)
  $selection.TypeText("Reading Acceptance Word")
  $selection.TypeParagraph()
  $selection.Style = $doc.Styles.Item(-3)
  $selection.TypeText("Introduction")
  $selection.TypeParagraph()
  $selection.Style = $doc.Styles.Item(-4)
  $selection.TypeText("Background")
  $selection.TypeParagraph()
  $selection.Style = $doc.Styles.Item(-3)
  $selection.TypeText("Results")
  $selection.TypeParagraph()
  $selection.Style = $doc.Styles.Item(-1)
  $selection.TypeText("Body text so the document has a paragraph to read.")
  $doc.SaveAs2($full, 12)
  $doc.Close(0)
} finally {
  $word.Quit()
}
Write-Output "wrote $full"
