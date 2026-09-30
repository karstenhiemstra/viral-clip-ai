from app.models import Job, JobStatus, JobType, Video
from app.services import queue


def test_enqueue_dedupes_active_jobs(db):
    v = Video(title="x")
    db.add(v)
    db.commit()
    a = queue.enqueue(db, JobType.ANALYZE_VIDEO, video_id=v.id, priority=10)
    b = queue.enqueue(db, JobType.ANALYZE_VIDEO, video_id=v.id, priority=90)
    assert a.id == b.id
    db.refresh(a)
    assert a.priority == 90
    c = queue.enqueue(db, JobType.RENDER_CLIP, video_id=v.id)
    assert c.id != a.id


def test_claim_order_and_lifecycle(db):
    low = queue.enqueue(db, JobType.TRAIN_MODEL, priority=10, dedupe=False)
    high = queue.enqueue(db, JobType.TRAIN_MODEL, priority=90, dedupe=False)
    first = queue.claim_next(db, "w1")
    assert first.id == high.id and first.status == JobStatus.RUNNING and first.attempts == 1
    queue.complete(first.id, "klaar")
    second = queue.claim_next(db, "w1")
    assert second.id == low.id
    assert queue.claim_next(db, "w1") is None

    queue.fail(second.id, "boom", retry_delay_seconds=0)
    db.expire_all()
    job = db.get(Job, second.id)
    assert job.status == JobStatus.QUEUED  # retried (max_attempts=2)
    again = queue.claim_next(db, "w1")
    queue.fail(again.id, "boom again")
    db.expire_all()
    assert db.get(Job, second.id).status == JobStatus.FAILED
    queue.retry(db, db.get(Job, second.id))
    assert db.get(Job, second.id).status == JobStatus.QUEUED


def test_waiting_jobs_wake_up_on_media(db):
    v = Video(title="x")
    db.add(v)
    db.commit()
    job = queue.enqueue(db, JobType.ANALYZE_VIDEO, video_id=v.id)
    claimed = queue.claim_next(db, "w")
    queue.mark_waiting(claimed.id, "wacht op media")
    db.expire_all()
    assert db.get(Job, job.id).status == JobStatus.WAITING
    assert queue.claim_next(db, "w") is None
    assert queue.wake_waiting_for_video(db, v.id) == 1
    assert queue.claim_next(db, "w").id == job.id


def test_cancel(db):
    job = queue.enqueue(db, JobType.TRAIN_MODEL)
    queue.cancel(db, job)
    assert job.status == JobStatus.CANCELLED
    assert queue.claim_next(db, "w") is None
