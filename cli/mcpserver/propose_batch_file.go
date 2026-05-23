package mcpserver

import (
	"context"
	"encoding/json"
	"fmt"
	"os"

	"github.com/mark3labs/mcp-go/mcp"
)

// ProposeBatchFileTool returns the MCP tool definition for propose_batch_file.
//
// propose_batch_file is the file-path sibling of propose_batch. The skill
// (/cq:reflect) writes its candidate list to a temp JSON file on disk and
// passes the absolute path here instead of inlining the full list as a
// tool-call argument. Net effect: the harness's echo of the tool call shrinks
// from ~5–6KB (one full candidate list with ~600B/candidate) to ~250B (one
// short path string), regardless of candidate count.
//
// Behavior of the per-candidate iteration is shared byte-identically with
// propose_batch — see (*Server).proposeCandidates. The response shape is a
// COMPACT variant that drops the per-stored Summary + Tier fields: the caller
// already has summaries locally (it wrote the file) and the tier is uniformly
// the configured tier. The per-candidate index is enough to key local lookups.
func ProposeBatchFileTool() mcp.Tool {
	return mcp.NewTool("propose_batch_file",
		mcp.WithDescription(
			"Propose multiple knowledge units in a single call by reading the candidate list "+
				"from a JSON file on disk. Sibling of propose_batch — same behavior, but the "+
				"candidate payload lives in a file rather than the tool-call argument, which "+
				"keeps the harness echo compact for flows like /cq:reflect. Each candidate has "+
				"the same shape as the propose tool's arguments. Partial success is allowed: "+
				"failures are reported per-candidate and do not abort the batch.",
		),
		mcp.WithString("candidates_path",
			mcp.Required(),
			mcp.Description("Absolute path to a JSON file containing an array of candidate KUs in the same shape as propose_batch's candidates argument."),
		),
	)
}

// batchStoredCompact is the compact stored-entry shape returned by
// propose_batch_file. Drops Summary + Tier from batchStored — the caller has
// summaries locally (it wrote the file) and tier is uniform.
type batchStoredCompact struct {
	Index   int    `json:"index"`
	ID      string `json:"id"`
	Warning string `json:"warning,omitempty"`
}

// batchResponseCompact is the JSON shape returned by propose_batch_file. It
// reuses batchError for the failure slice (failures carry per-candidate
// summary because the caller's lookup-by-index path doesn't help when the
// candidate never reached the propose call).
type batchResponseCompact struct {
	Stored []batchStoredCompact `json:"stored"`
	Errors []batchError         `json:"errors"`
	// LocalFallbackCount mirrors batchResponse — number of candidates that
	// fell back to local storage.
	LocalFallbackCount int `json:"local_fallback_count,omitempty"`
	// LocalFallbackReason mirrors batchResponse — human-readable reason
	// captured from the first FallbackError encountered.
	LocalFallbackReason string `json:"local_fallback_reason,omitempty"`
}

// HandleProposeBatchFile is the file-path entry point for propose_batch.
// Reads candidates from a JSON file at the given absolute path and runs the
// same per-candidate validation + propose loop as HandleProposeBatch, then
// returns a compact response (no Summary or Tier on stored entries).
func (s *Server) HandleProposeBatchFile(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	args := req.GetArguments()
	rawPath, ok := args["candidates_path"]
	if !ok {
		return mcp.NewToolResultError("candidates_path is required"), nil
	}
	path, ok := rawPath.(string)
	if !ok {
		return mcp.NewToolResultError("candidates_path must be a string"), nil
	}
	if path == "" {
		return mcp.NewToolResultError("candidates_path is required"), nil
	}

	// Plain ReadFile — no symlink chasing or path normalisation. The MCP
	// server runs as the operator's user and there is no privilege
	// boundary; surprising symlink targets would be the operator's own
	// files. Keep this boring on purpose.
	data, err := os.ReadFile(path) //nolint:gosec // path is operator-supplied; no privilege escalation here.
	if err != nil {
		return mcp.NewToolResultError(fmt.Sprintf("reading candidates_path %q: %v", path, err)), nil
	}

	// Decode as a permissive any first so we can distinguish "malformed
	// JSON" (parse error) from "well-formed JSON but the wrong shape" (not
	// an array). Both surface as tool errors but the operator-facing
	// message differs.
	var raw any
	if err := json.Unmarshal(data, &raw); err != nil {
		return mcp.NewToolResultError(fmt.Sprintf("parsing candidates_path %q as JSON: %v", path, err)), nil
	}
	candList, ok := raw.([]any)
	if !ok {
		return mcp.NewToolResultError(fmt.Sprintf("candidates must be an array (candidates_path %q)", path)), nil
	}

	full := s.proposeCandidates(ctx, candList)

	// Project the full batchResponse onto the compact wire shape.
	compact := batchResponseCompact{
		Stored:              make([]batchStoredCompact, 0, len(full.Stored)),
		Errors:              full.Errors,
		LocalFallbackCount:  full.LocalFallbackCount,
		LocalFallbackReason: full.LocalFallbackReason,
	}
	for _, s := range full.Stored {
		compact.Stored = append(compact.Stored, batchStoredCompact{
			Index:   s.Index,
			ID:      s.ID,
			Warning: s.Warning,
		})
	}
	if compact.Errors == nil {
		compact.Errors = []batchError{}
	}

	out, err := json.Marshal(compact)
	if err != nil {
		return nil, fmt.Errorf("encoding batch result: %w", err)
	}

	return mcp.NewToolResultText(string(out)), nil
}
