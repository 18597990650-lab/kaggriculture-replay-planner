"""Attach the new output's depot tasks to a generated crop replacement plan."""
from .programmes import ProductionProgramme,Task


def with_output_deliveries(parent, candidate, observation):
    """Keep original tasks and labor, charging each extra depot visit in the compiler.

    A new harvest product follows the old wheat deposit within its actor's tour.
    A tour without an existing matching deposit or DROP receives an explicit
    final depot task. Empty bags make that task obsolete at runtime.
    """
    start = int(observation['step'])//24
    old_plantings = {(t.point,t.item,t.generation) for actors in parent.days
                     for sequence in actors.values() for t in sequence if t.operation=='PLANT'}
    added = {(t.point,t.item,t.generation) for actors in candidate.days
             for sequence in actors.values() for t in sequence
             if t.operation=='PLANT' and (t.point,t.item,t.generation) not in old_plantings}
    if not added:
        return candidate,dict(added_plantings=[],added_deposits=[])
    days = []
    deposits = []
    for day,actors in enumerate(candidate.days):
        if day<start:
            days.append(dict(actors))
            continue
        rows = {}
        for actor,sequence in actors.items():
            tasks = []
            pending = set()
            for task in sequence:
                tasks.append(task)
                if task.operation=='HARVEST' and (task.point,task.item,task.generation) in added:
                    pending.add(task.item)
                if task.operation=='DROP':
                    pending.clear()
                if task.operation=='DEPOSIT':
                    pending.discard(task.item)
                    if task.item=='WHEAT':
                        for item in sorted(pending):
                            tasks.append(Task(task.point,'DEPOSIT',item,None))
                            deposits.append(dict(day=day,actor=actor,item=item,placement='after_wheat_deposit'))
                        pending.clear()
            for item in sorted(pending):
                tasks.append(Task((4,4),'DEPOSIT',item,None))
                deposits.append(dict(day=day,actor=actor,item=item,placement='end_of_tour'))
            rows[actor] = tuple(tasks)
        days.append(rows)
    result = ProductionProgramme(candidate.name+'-output-delivery',tuple(days),candidate.workers)
    assert result.days[:start]==parent.days[:start]
    assert result.workers==parent.workers
    for day,actors in enumerate(candidate.days):
        for actor,sequence in actors.items():
            iterator = iter(result.days[day][actor])
            assert all(any(task==other for other in iterator) for task in sequence)
    return result,dict(added_plantings=[dict(point=point,item=item,generation=generation)
        for point,item,generation in sorted(added)],added_deposits=deposits)
