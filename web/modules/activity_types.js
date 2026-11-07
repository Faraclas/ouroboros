// Browser twin of gateway/activity_contracts.py. History never authorizes controls.
/**
 * @typedef {Object} ActivityAssessment
 * @property {string} state
 * @property {string} source_hash
 * @property {string} reason
 * @property {Object<string, string>} by
 * @property {string} at
 * @property {string=} investigating_task_id
 * @property {boolean=} declared_error
 */
/**
 * @typedef {Object} ActivityEntry
 * @property {string} key
 * @property {string} kind
 * @property {string} task_id
 * @property {string} name
 * @property {*} at
 * @property {string} outcome
 * @property {boolean} error
 * @property {string} source_hash
 * @property {string=} phase
 * @property {Object<string, *>=} service
 * @property {ActivityAssessment=} assessment
 */
/**
 * @typedef {Object} ActivityTask
 * @property {string} task_id
 * @property {*} at
 * @property {Array<ActivityEntry>} entries
 * @property {Array<string>} gaps
 */
/**
 * @typedef {Object} ActivityCoverage
 * @property {number} scanned
 * @property {boolean} reached_start
 * @property {boolean} bindings_stamped
 * @property {number} candidate_budget
 */
/**
 * @typedef {Object} ActivityPageResponse
 * @property {number} room
 * @property {string} view
 * @property {Array<ActivityTask>} tasks
 * @property {*} next_cursor
 * @property {string} observed_at
 * @property {Array<string>} gaps
 * @property {ActivityCoverage} coverage
 */
export {};
