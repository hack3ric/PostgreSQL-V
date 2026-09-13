#ifndef VECTOR_INDEX_WORKER_H
#define VECTOR_INDEX_WORKER_H

#include "postgres.h"
#include "storage/proc.h"   
#include "ringbuffer.h"

#ifndef MAX_SEGMENTS_SIZE
#define MAX_SEGMENTS_SIZE                2000000
#endif
#ifndef THRESHOLD_SMALL_SEGMENT_SIZE
#define THRESHOLD_SMALL_SEGMENT_SIZE     1000000
#endif

void vector_index_worker_init(void);
PGDLLEXPORT void vector_index_worker_main(Datum main_arg);


// typedef struct {
//     PGPROC *volatile client_proc;
//     PGPROC *volatile worker_proc;  /* new: set by worker at startup */
// } TaskDesc;

#endif
