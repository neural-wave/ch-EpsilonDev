# Virtuoso setup

The knowledge base runs in Docker with the organizers' release scripts. We keep two databases:

| | Dev | Perf |
|---|---|---|
| Patients | 2,000 | 100,000 |
| Endpoint | `http://localhost:8890/sparql` | `http://localhost:8891/sparql` |
| Use | development, profiling, eval correctness | query timing at the jury's scale |

## First time

1. Install a Docker runtime. On a Mac without Docker Desktop:
   `brew install colima docker && colima start --cpu 12 --memory 28 --disk 200 --vm-type vz --mount-type virtiofs`
2. Unzip `hackathon-virtuoso-release.zip` next to this repo. Put a subset of patient files in its `patients/` folder (we used the first 2,000 of `gen_mock_data-1_1000001-1100000.tar.zst`).
3. `RELEASE_DIR=../hackathon-virtuoso-release ./infra/virtuoso/dev.sh load`. Loading the ontologies takes about 4 minutes; 2,000 patients take 15 seconds.
4. For perf, extract all 100k patients into their own folder, then
   `RELEASE_DIR=... PERF_PATIENTS_DIR=... ./infra/virtuoso/perf.sh load` (about 80 minutes).

## After a reboot

`colima start`, then the same scripts without `load`: the containers keep their data.

## Things to know

- Use the HTTP endpoint. The release's `query.sh` goes through `isql`, which rejects some valid SPARQL (examples #06 and #15).
- The ontologies and patient files are restricted: never copy them into this repo.
