import gc

from copy import deepcopy
from itertools import groupby, product, count
from collections import defaultdict
from typing import Iterable, Sequence, Any, Optional, Dict, Hashable, Tuple

from coba.random import CobaRandom
from coba.learners import Learner, SafeLearner
from coba.config import CobaConfig
from coba.utilities import PackageChecker
from coba.pipes import Source, Pipe, Filter, IdentityFilter
from coba.simulations import Simulation, Interaction, Shuffle, Take, OpenmlSimulation, ClassificationSimulation
from coba.encodings import InteractionTermsEncoder

from coba.benchmarks.transactions import Transaction
from coba.benchmarks.results import Result

class Identifier():
    
    def __init__(self) -> None:
        self._source_ids   : Dict[Hashable, int]  = defaultdict(lambda x=count(): next(x)) # type: ignore
        self._learner_ids  : Dict[Hashable, int]  = defaultdict(lambda x=count(): next(x)) # type: ignore
        self._simulaion_ids: Dict[Hashable, int]  = defaultdict(lambda x=count(): next(x)) # type: ignore

    def id(self, simulation: Simulation, learner: Learner) -> Tuple[int,int,int]:
        source = simulation._source if isinstance(simulation, Pipe.SourceFilters) else simulation
        
        src_id = self._source_ids[source]
        sim_id = self._simulaion_ids[simulation]
        lrn_id = self._learner_ids[learner] if learner else None
        
        return (src_id, sim_id, lrn_id)

class Task(Filter[Iterable[Interaction], Iterable[Any]]):

    def __init__(self, src_id:int, sim_id: int, lrn_id: int, simulation: Simulation, learner: Optional[Learner]) -> None:

        self.sim_pipe   = simulation
        self.sim_source = simulation._source if isinstance(simulation, Pipe.SourceFilters) else simulation
        self.sim_filter = simulation._filter if isinstance(simulation, Pipe.SourceFilters) else IdentityFilter()
        self.learner    = SafeLearner(learner) if learner else None

        self.src_id = src_id
        self.sim_id = sim_id
        self.lrn_id = lrn_id

class EvaluationTask(Task):

    def __init__(self, src_id:int, sim_id: int, lrn_id: int, simulation: Simulation, learner: Learner, seed: int) -> None:
        self._seed = seed
        super().__init__(src_id, sim_id, lrn_id, simulation, learner)

    def filter(self, interactions: Iterable[Interaction]) -> Iterable[Any]:

        if not interactions: return

        learner = deepcopy(self.learner)
        random  = CobaRandom(self._seed)

        with CobaConfig.Logger.time(f"Evaluating learner {self.lrn_id} on Simulation {self.sim_id}..."):

            row_data = defaultdict(list)

            for interaction in interactions:

                context   = interaction.context
                actions   = interaction.actions
                feedbacks = interaction.feedbacks

                probs,info  = learner.predict(context, actions)

                action = random.choice(actions, probs)
                reward = feedbacks[actions.index(action)]
                prob   = probs[actions.index(action)]

                info = learner.learn(context, action, reward, prob, info) or {}
                                                        
                for key,value in info.items() | {('reward',reward)}: 
                    row_data[key].append(value)

            yield Transaction.interactions(self.sim_id, self.lrn_id, _packed=row_data)

class SimulationTask(Task):

    def filter(self, interactions: Iterable[Interaction]) -> Iterable[Any]:

        with CobaConfig.Logger.time(f"Calculating Simulation {self.sim_id} statistics..."):
            extra_statistics = {}

            contexts,actions,feedbacks = zip(*[ (i.context, i.actions, i.feedbacks) for i in interactions])

            if isinstance(self.sim_source, (ClassificationSimulation,OpenmlSimulation)):

                try:
                    PackageChecker.sklearn("")

                    from sklearn.feature_extraction import FeatureHasher
                    from sklearn.ensemble import RandomForestClassifier
                    from sklearn.tree import DecisionTreeClassifier
                    from sklearn.model_selection import cross_val_score

                    encoder = InteractionTermsEncoder('x')

                    X   = [ encoder.encode(x=c, a=[]) for c in contexts ]
                    y   = [ a[f.index(1)] for a,f in zip(actions,feedbacks)]
                    clf = DecisionTreeClassifier(random_state=1)

                    if isinstance(X[0][0],tuple):
                        X = FeatureHasher(n_features=2**14, input_type="pair").fit_transform(X)

                    if len(y) > 5:
                        extra_statistics["bayes_rate"] = round(cross_val_score(clf, X, y, cv=5).mean(),4)

                except ImportError:
                    pass

                labels     = set()
                features   = set() 
                label_cnts = defaultdict(int)

                for c,a,f in zip(contexts,actions,feedbacks):

                    inter_label = a[f.index(1)]
                    inter_feats = c.keys() if isinstance(c,dict) else range(len(c))

                    labels.add(inter_label)
                    features.update(inter_feats)
                    label_cnts[inter_label] += 1

                extra_statistics["action_cardinality"] = len(labels)
                extra_statistics["context_dimensions"] = len(features)
                extra_statistics["imbalance_ratio"]    = round(max(label_cnts.values())/min(label_cnts.values()),4)

            if isinstance(self.sim_filter,Pipe.FiltersFilter):
                filters = self.sim_filter._filters
            elif isinstance(self.sim_filter, IdentityFilter):
                filters = []
            else:
                filters = [self.sim_filter]

            source  = str(self.sim_source).strip('"')
            shuffle = "None"
            take    = "None"
            pipe    = str(self.sim_pipe) 

            for filter in filters:
                if isinstance(filter, Shuffle): shuffle = str(filter._seed )
                if isinstance(filter, Take   ): take    = str(filter._count)

            yield Transaction.simulation(self.sim_id, 
                source=source, 
                shuffle=shuffle, 
                take=take, 
                pipe=pipe, 
                **extra_statistics
            )

class CreateTasks(Source[Iterable[Task]]):

    def __init__(self, simulations: Sequence[Simulation], learners: Sequence[Learner], seed: int = None) -> None:
        self._simulations = simulations
        self._learners    = learners
        self._seed        = seed

    def read(self) -> Iterable[Task]:

        #we rely on sim_id to make sure we don't do duplicate work. So long as self._simulations
        #is always in the exact same order we should be fine. In the future we may want to consider.
        #adding a better check for simulations other than assigning an index based on their order.

        identifier = Identifier()

        for simulation in self._simulations:
            yield SimulationTask(*identifier.id(simulation, None), simulation, None)

        for simulation, learner in product(self._simulations, self._learners):
            yield EvaluationTask(*identifier.id(simulation, learner), simulation, learner, self._seed)

class FilterFinished(Filter[Iterable[Task], Iterable[Task]]):
    def __init__(self, restored: Result) -> None:
        self._restored = restored

    def filter(self, tasks: Iterable[Task]) -> Iterable[Task]:

        def is_not_complete(task: Task):

            if isinstance(task,SimulationTask):
                return task.sim_id not in self._restored.simulations

            if isinstance(task,EvaluationTask):
                return (task.sim_id,task.lrn_id) not in self._restored._interactions

            raise Exception("Unrecognized Task")

        return filter(is_not_complete, tasks)

class ChunkBySource(Filter[Iterable[Task], Iterable[Iterable[Task]]]):

    def filter(self, tasks: Iterable[Task]) -> Iterable[Iterable[Task]]:

        srt_key = lambda t: t.src_id
        grp_key = lambda t: t.src_id

        tasks = list(tasks)

        for _, group in groupby(sorted(tasks, key=srt_key), key=grp_key):
            yield list(group)

class ChunkByTask(Filter[Iterable[Task], Iterable[Iterable[Task]]]):

    def filter(self, tasks: Iterable[Task]) -> Iterable[Iterable[Task]]:

        for task in tasks:
            yield [ task ]

class ChunkByNone(Filter[Iterable[Task], Iterable[Iterable[Task]]]):

    def filter(self, tasks: Iterable[Task]) -> Iterable[Iterable[Task]]:
        yield list(tasks)

class ProcessTasks(Filter[Iterable[Iterable[Task]], Iterable[Any]]):

    def filter(self, chunks: Iterable[Iterable[Task]]) -> Iterable[Any]:

        for chunk in chunks:
            for transaction in self._process_chunk(chunk):
                yield transaction

    def _process_chunk(self, task_group: Iterable[Task]) -> Iterable[Any]:

        source_by_id = { t.src_id: t.sim_source for t in task_group }
        filter_by_id = { t.sim_id: t.sim_filter for t in task_group }

        srt_src = lambda t: t.src_id
        grp_src = lambda t: t.src_id
        srt_sim = lambda t: t.sim_id
        grp_sim = lambda t: t.sim_id

        with CobaConfig.Logger.log(f"Processing chunk..."):

            for src_id, tasks_for_src in groupby(sorted(task_group, key=srt_src), key=grp_src):

                try:

                    tasks_for_src = list(tasks_for_src)
                    sim_cnt_for_src = len(set([task.sim_id for task in tasks_for_src]))

                    with CobaConfig.Logger.time(f"Creating source {src_id} from {source_by_id[src_id]}..."):
                        #This is not ideal. I'm not sure how it should be improved so it is being left for now.
                        #Maybe add a flag to the Benchmark to say whether the source should be stashed in mem?
                        loaded_source = list(source_by_id[src_id].read())

                    for sim_id, tasks_for_sim in groupby(sorted(tasks_for_src, key=srt_sim), key=grp_sim):

                        with CobaConfig.Logger.time(f"Creating simulation {sim_id} from source {src_id}..."):
                            interactions = list(filter_by_id[sim_id].filter(loaded_source))

                        if sim_cnt_for_src == 1:
                            #this will hopefully help with memory...
                            loaded_source = None
                            gc.collect()

                        if not interactions:
                            CobaConfig.Logger.log(f"Simulation {sim_id} has nothing to evaluate (likely due to `take` being larger than the simulation).")
                            return

                        for task in tasks_for_sim:
                            try:
                                for transaction in task.filter(interactions): 
                                    yield transaction
                            except Exception as e:
                                CobaConfig.Logger.log_exception(e)

                except Exception as e:
                    CobaConfig.Logger.log_exception(e)