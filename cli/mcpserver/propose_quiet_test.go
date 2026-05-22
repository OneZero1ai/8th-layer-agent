package mcpserver

import (
	"context"
	"errors"
	"testing"

	"github.com/mark3labs/mcp-go/mcp"
	"github.com/stretchr/testify/require"

	cq "github.com/mozilla-ai/cq/sdk/go"
)

// These tests mutate process env via t.Setenv and therefore cannot run in
// parallel with the rest of the propose/propose_batch tests (which set
// t.Parallel on the outer test). Keeping them in a dedicated test function
// avoids the panic Go's testing package raises when t.Setenv is called from
// a parallel-marked test tree.

func TestProposeQuietFallback_LegacyEnvelopeWhenDisabled(t *testing.T) {
	t.Setenv("CQ_QUIET_LOCAL_FALLBACK", "false")

	localUnit := cq.KnowledgeUnit{ID: "ku_legacy_envelope_1234567890abcdef"}
	s := New(&mockClient{
		proposeFn: func(_ context.Context, _ cq.ProposeParams) (cq.KnowledgeUnit, error) {
			return cq.KnowledgeUnit{}, &cq.FallbackError{
				LocalUnit: localUnit,
				Err:       errors.New("remote API unreachable: connection refused"),
			}
		},
	}, "test")

	result, err := s.HandlePropose(context.Background(), mcp.CallToolRequest{
		Params: mcp.CallToolParams{
			Name: "propose",
			Arguments: map[string]any{
				"summary": "s", "detail": "d", "action": "a",
				"domains": []any{"api"},
			},
		},
	})
	require.NoError(t, err)
	require.False(t, result.IsError)

	text := result.Content[0].(mcp.TextContent).Text
	require.Contains(t, text, "warning: stored locally after remote failure")
	require.Contains(t, text, "ku_legacy_envelope_1234567890abcdef")
}

func TestProposeBatchQuietFallback_LegacyWarningWhenDisabled(t *testing.T) {
	t.Setenv("CQ_QUIET_LOCAL_FALLBACK", "false")

	s := New(&mockClient{
		proposeFn: func(_ context.Context, _ cq.ProposeParams) (cq.KnowledgeUnit, error) {
			return cq.KnowledgeUnit{}, &cq.FallbackError{
				LocalUnit: cq.KnowledgeUnit{ID: "ku_legacy_warn", Tier: cq.Local},
				Err:       errors.New("remote API unreachable: connection refused"),
			}
		},
	}, "test")

	result, err := s.HandleProposeBatch(context.Background(), mcp.CallToolRequest{
		Params: mcp.CallToolParams{
			Name: "propose_batch",
			Arguments: map[string]any{
				"candidates": []any{
					map[string]any{"summary": "x", "detail": "d", "action": "a", "domains": []any{"x"}},
				},
			},
		},
	})
	require.NoError(t, err)

	resp := decodeBatchResult(t, result)
	require.Len(t, resp.Stored, 1)
	require.Contains(t, resp.Stored[0].Warning, "stored locally after remote failure")
	// Roll-up fields populate regardless of quiet mode so the caller always
	// knows how many candidates fell back.
	require.Equal(t, 1, resp.LocalFallbackCount)
	require.Contains(t, resp.LocalFallbackReason, "stored locally after remote failure")
}

func TestQuietLocalFallback_EnvVarParsing(t *testing.T) {
	cases := []struct {
		name  string
		value string
		want  bool
	}{
		{"unset", "", true},
		{"empty", "", true},
		{"true", "true", true},
		{"True", "True", true},
		{"1", "1", true},
		{"yes", "yes", true},
		{"false", "false", false},
		{"FALSE", "FALSE", false},
		{"0", "0", false},
		{"no", "no", false},
		{"off", "off", false},
		{"random", "banana", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("CQ_QUIET_LOCAL_FALLBACK", tc.value)
			require.Equal(t, tc.want, quietLocalFallback())
		})
	}
}
