// Catalog-priced projections are not provider billing or wallet amounts.
export function catalogEstimateText(estimate) {
    if (!estimate || typeof estimate !== 'object') return '';
    const {amount, catalog_tier: tier, crossed_default_threshold: crossed, has_long_context_surcharge: surcharge,
        default_max_prompt_tokens: threshold, unknown_reason: reason} = estimate;
    const estimateText = Number.isFinite(amount) && amount >= 0
        ? `Copilot catalog estimate: ${amount.toLocaleString(undefined, {maximumSignificantDigits: 6})} catalog units (not billed cost)`
        : 'Copilot catalog estimate: unknown';
    const tierText = tier === 'default' ? 'Estimated standard catalog tier' : tier === 'long_context'
        ? `Estimated long-context catalog tier${surcharge === true ? ' (surcharge advertised)'
            : surcharge === false ? ' (no surcharge advertised)' : ''}` : '';
    const boundary = Number.isSafeInteger(threshold) && threshold > 0 && typeof crossed === 'boolean'
        ? `${crossed ? 'Above' : 'At or below'} the ${threshold.toLocaleString()}-token standard threshold`
        : '';
    const unknown = {
        no_current_catalog: 'No current catalog for this route and account',
        unbound_response_model: 'Response model could not be matched to the catalog',
        unknown_catalog_tier: 'Catalog tier cannot be determined',
        unreported_token_counts: 'Token counts not fully reported',
        unreported_cache_write_split: 'Cache-write split not fully reported',
        inconsistent_input_split: 'Inconsistent reported input split',
        inconsistent_cache_write_split: 'Inconsistent reported cache-write split',
        unknown_catalog_rate: 'Catalog rate missing',
        unknown_catalog_batch_size: 'Catalog batch size missing',
        out_of_range_estimate: 'Estimate out of range',
    }[reason];
    const assumption = estimate.cache_write_pricing === 'unsplit_rate_assumed'
        ? 'Unsplit cache writes estimated at the published write rate' : '';
    return [estimateText, tierText, boundary, assumption, unknown].filter(Boolean).join(' · ');
}
