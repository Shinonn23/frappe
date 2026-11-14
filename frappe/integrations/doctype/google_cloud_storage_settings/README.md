# Google Cloud Storage file backend

The Settings form configures a private bucket with Public Access Prevention
explicitly enforced and a unique object prefix per site. Frappe uses Google
Application Default Credentials; its service account needs bucket metadata access
and object create, read, list, and delete permissions. **Test Connection** checks
these prerequisites. The first copy claims the prefix with a site-owner object;
a different site or an unowned pre-existing object is rejected.

Chunked uploads use session-bound temporary objects under `<prefix>/.uploads/`.
The application deletes them after the File transaction commits; a daily job
cleans abandoned uploads after 24 hours. The assembled file is held in memory and
remains subject to the configured maximum file size. API clients sending multiple
chunks must send a stable UUID `upload_id`, `total_file_size`, and unchanged
attachment parameters with every chunk.

Package imports extract temporarily into `/dev/shm` and remove the workspace
afterwards. The host needs enough tmpfs capacity for the extracted package;
the configured maximum ZIP extract size also limits package extraction.
Local Chromium PDF rendering uses a temporary profile and cache on `/dev/shm`.
External Chromium and interactive Chromium debugging are unavailable while this
backend is enabled because their storage location cannot be enforced here.

## Preparing a cutover

1. Configure `gcs_coordinated_cutover` as `1` for the site, deploy this code and
   restart all web and worker processes. All participating processes must use
   the same bench checkout and `/dev/shm` on a single host.
2. Pause the scheduler. Stop old workers, other hosts and external file writers;
   leave a long worker running to execute the migration. In-flight guarded
   requests and jobs prevent the exclusive cutover lock from being acquired.
3. Configure and reload Nginx so `/files/` is forwarded to Frappe and proxy
   request and response buffering are disabled. `bench setup nginx` regenerates
   its configuration; after regenerating it, run the following from the bench
   directory, then review the diff and test before reloading Nginx:

   ```sh
   ./env/bin/python -m frappe.integrations.doctype.google_cloud_storage_settings.nginx config/nginx.conf --write
   sudo nginx -t
   ```

   This utility handles bench-generated `@webserver` locations and refuses
   conflicting `/files/` upstreams. Review custom Nginx locations separately,
   especially routes that bypass `@webserver`; they may need the same buffering
   settings. Reload Nginx only after deploying and restarting the matching Frappe
   code. Running the utility without `--write` only checks for pending changes.
4. Save the bucket and prefix, use **Copy Existing Files**, and inspect its job
   result. If source files were deleted during copying, **Reconcile Copied
   Files** removes only stale objects owned by this site's migration. **Run
   Preflight** checks incomplete uploads, File references, privacy and the
   copied snapshot. Preflight is point-in-time and does not activate storage.
5. Confirm the process isolation and Nginx routing checkboxes in Settings, then
   select **Activate Storage**. The job acquires an exclusive lock, starts a
   fresh database snapshot, repeats validation and copying, saves a cleanup plan
   in the bucket, commits the storage switch, and then removes only verified
   local files. Resume the scheduler and other processes after the job completes.

Refresh Settings after a successful job to see **Last Migration Result**. Failed
jobs and their errors remain in Background Jobs; a previous successful result
does not mean a later failed job succeeded.

If cleanup fails after activation, storage remains enabled and the saved plan can
be resumed with **Resume Local Cleanup** while the same isolation precautions are
in place. Changed source files or bucket object generations are retained and
reported for investigation. The backend cannot currently be disabled or pointed
at a different bucket without a separately verified reverse migration.
Keep `gcs_coordinated_cutover` enabled after activation. Per-object locks use it
to coordinate attachment references, privacy changes and deletion through the
end of each transaction.
While this guard is enabled, maintenance-mode requests use a read-only primary
database transaction even if `read_from_replica` is set, so a lagging replica
cannot serve outdated storage settings or file permissions.

File overwrites made inside a database savepoint are restored on savepoint
rollback using generation preconditions. If that restoration fails, the
transaction cannot commit and must be rolled back fully. Database and object
storage do not share a durable transaction: a process crash during an overwrite
can still require reconciliation.

After activation, **Check File Integrity** compares each local File record with
the current bucket object without changing either. Results contain counts and
up to 100 example File names in **Last Integrity Check** after refreshing the
Settings form; records without a content hash and remote files are skipped. The scan reads object contents and may incur network and bucket
charges. It can detect a mismatch, but does not determine which version is right
or recover an overwritten version.

The host-local lock cannot protect against old processes, other hosts, direct
filesystem writers, or an incorrectly configured proxy. The confirmations are
operator assertions, not automatic proof of those conditions. This implementation
has not yet been validated with a real GCP bucket or a production site.

The opt-in `test_site_isolated` integration test creates a disposable Frappe
site, MariaDB and Redis under `/dev/shm`, and uses a local GCS API stub. Run it
from `sites` with `FRAPPE_GCS_TEST_SITE=1` and the bench Python interpreter. It
does not require or contact a real bucket and does not use existing site data.
